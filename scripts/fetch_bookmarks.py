#!/usr/bin/env python3
"""Xのブックマーク一覧を取得する。

3段構成:
  1. Service Worker無効化 + context.on("response") でGraphQL傍受
  2. DOM直接スクレイピング（fallback）
  3. 両方の結果をマージ

使い方（influxディレクトリで実行、またはDockerコンテナ内）:
  python scripts/fetch_bookmarks.py --out output/bookmarks.jsonl
  python scripts/fetch_bookmarks.py --out output/bookmarks.jsonl --max-scrolls 10
  python scripts/fetch_bookmarks.py --out output/bookmarks.jsonl --max-empty-batches 5 --max-runtime-min 30
  python scripts/fetch_bookmarks.py --out output/bookmarks.jsonl --checkpoint output/checkpoint.json
  docker exec xstock-vnc python scripts/fetch_bookmarks.py --out /app/output/bookmarks.jsonl

履歴: 2026-07-11 --appendモードのデータ消失バグを修正。本文補完(backfill)後の
出力ファイル再書き込みが今回取得分のみで全体を上書きしていた不具合に対し、
起動時に既存レコード全件を読み込み、終了時は「既存∪今回取得」を一時ファイル
経由で原子的に置換する方式（件数不減少ガード付き）に変更。
履歴: 2026-07-12 --status-json PATH を追加。実行終了時にsaved/dom_count/
graphql_count/stopped_reasonをJSONで書き出す。exit 0でもdom_count==0
（selector死・ログイン壁等でtweetが1個も観測できない）ケースを呼び出し側が
SUCCESSと誤分類しないよう検知できるようにするため。
履歴: 2026-08-19 数値指標（like/retweet/reply/bookmark/view）の計測を廃止し
全フィールド null 固定に変更（敵対レビュー 2026-08-19 一致結論・オーナー承認）。
理由: ①「万」表記のパース不能で例外→0 を保存する全損（3万いいね→0 の実証4件）
②write-once ゲートで初回0が永久凍結 ③下流の消費者は url/author/text しか
使っていない（数値は grok-twittora 経路が正）。X指標が要る時は
scripts/x_metrics_lib.py（オンデマンド計測・captured_at 付き）を使うこと。
過去に保存済みの数値（0埋め）は書き換えず残すが参照禁止
（正本の宣言= make_article/docs/data-sources.md §計測正本の対応表）。
"""

import argparse
import json
import logging
import os
import random
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@dataclass
class Bookmark:
    url: str = ""
    text: str = ""
    author: str = ""
    # 数値指標は 2026-08-19 に計測廃止（欠測= null 固定・0 を書かない則）。
    # フィールド自体を残すのは既存 jsonl とのスキーマ互換のため。
    like_count: Optional[int] = None
    retweet_count: Optional[int] = None
    reply_count: Optional[int] = None
    bookmark_count: Optional[int] = None
    view_count: Optional[int] = None
    is_long_form: bool = False
    created_at: str = ""


from collector.exceptions import CookieExpiredError


def load_cookies(profile_path: str = "./x_profile") -> list:
    """x_profile/cookies.json から Cookie を読み込む（暗号化対応）。

    Raises:
        CookieExpiredError: cookies.json が存在しない／空の場合。
    """
    from collector.cookie_crypto import load_cookies_or_raise
    return load_cookies_or_raise(Path(profile_path).resolve() / "cookies.json")


# ── チェックポイント管理 ───────────────────────────────────────

def load_checkpoint(checkpoint_path: Optional[str]) -> dict:
    """チェックポイントファイルからseen_urlsと進捗情報を復元する。"""
    if not checkpoint_path:
        return {"seen_urls": set(), "total_count": 0, "last_scroll": 0}

    cp_file = Path(checkpoint_path)
    if not cp_file.exists():
        return {"seen_urls": set(), "total_count": 0, "last_scroll": 0}

    try:
        with open(cp_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        seen = set(data.get("seen_urls", []))
        logger.info("チェックポイント復元: %d件のURLをスキップ対象に設定", len(seen))
        return {
            "seen_urls": seen,
            "total_count": data.get("total_count", 0),
            "last_scroll": data.get("last_scroll", 0),
        }
    except Exception as exc:
        logger.warning("チェックポイント読込失敗: %s", exc)
        return {"seen_urls": set(), "total_count": 0, "last_scroll": 0}


def save_checkpoint(checkpoint_path: Optional[str], seen_urls: set, total_count: int, scroll: int) -> None:
    """チェックポイントを保存する。"""
    if not checkpoint_path:
        return
    try:
        cp_file = Path(checkpoint_path)
        cp_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "seen_urls": sorted(seen_urls),
            "total_count": total_count,
            "last_scroll": scroll,
            "last_updated": datetime.now().isoformat(),
        }
        with open(cp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("チェックポイント保存失敗: %s", exc)


# ── 追記マージ & 原子的書き出し ──────────────────────────────

class BookmarkCountRegressionError(RuntimeError):
    """マージ後の件数が既存件数を下回った場合に送出する（データ消失ガード）。"""


def _write_status_json(
    path: str,
    dom_bookmarks: Dict[str, "Bookmark"],
    graphql_bookmarks: Dict[str, "Bookmark"],
    all_bookmarks: List["Bookmark"],
    stopped_reason: str,
) -> None:
    """実行結果サマリをJSONで書き出す（呼び出し側がexit 0でも0件取得を検知できるように）。

    stopped_reason: "max_runtime" | "max_empty" | "end" | "error"
    observed_url_count: 今回の実行中にDOM+GraphQLの両ソースで観測したユニークURL数
        （既存ファイルに既にあった既知URLも含む。新規保存分のみのsavedとは別軸で、
        「部分取得（既知URLの再表示だけで巡回が浅い）」をwrapper側が検知するための指標）。
    """
    observed_url_count = len(set(dom_bookmarks) | set(graphql_bookmarks))
    payload = {
        "saved": len(all_bookmarks),
        "dom_count": len(dom_bookmarks),
        "graphql_count": len(graphql_bookmarks),
        "observed_url_count": observed_url_count,
        "stopped_reason": stopped_reason,
    }
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception as exc:
        logger.warning("status-json書き出し失敗: %s", exc)


def merge_bookmark_records(existing: List[dict], fetched: List[dict]) -> List[dict]:
    """既存レコードと今回取得レコードをURLをキーにマージする。

    既存の並び順を維持したまま、同一URLは今回取得レコードで上書きする
    （テキスト補完/backfillの結果を反映するため）。既存にしかないURLは必ず保持する。
    URLを持たないレコードは（本スクリプトでは書き出し対象にならないため）無視する。
    """
    merged: Dict[str, dict] = {}
    order: List[str] = []
    for rec in existing:
        url = rec.get("url")
        if not url:
            continue
        if url not in merged:
            order.append(url)
        merged[url] = rec
    for rec in fetched:
        url = rec.get("url")
        if not url:
            continue
        if url not in merged:
            order.append(url)
        merged[url] = rec
    return [merged[url] for url in order]


def atomic_write_jsonl(path: str, records: List[dict], min_count: int = 0) -> None:
    """recordsをJSONLとして同一ディレクトリの一時ファイルに書き出し、
    flush+fsync後にos.replaceで原子的に置換する。

    件数不減少ガード: len(records) < min_count の場合は元ファイルに一切触れず
    BookmarkCountRegressionError を送出する。
    """
    if len(records) < min_count:
        raise BookmarkCountRegressionError(
            f"件数不減少ガード発動: 書き込み予定件数({len(records)})が既存件数({min_count})を"
            f"下回るため、出力ファイルへの書き込みを中止しました: {path}"
        )
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(p))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── DOM スクレイピング（メイン取得方法）──────────────────────

def scrape_bookmarks_from_dom(page) -> List[Bookmark]:
    """ページに表示されているブックマークをDOMから直接取得する。

    GraphQL傍受に依存しない最も確実な方法。
    """
    results = []
    seen_urls = set()

    cards = page.locator('[data-testid="tweet"]')
    count = cards.count()

    for i in range(count):
        try:
            card = cards.nth(i)

            # テキスト（inner_textで確実にレンダリング済みテキストを取得）
            text = ""
            text_el = card.locator('[data-testid="tweetText"]')
            if text_el.count():
                try:
                    text = text_el.first.inner_text(timeout=3000) or ""
                except Exception:
                    text = text_el.first.text_content() or ""

            # URL（/status/リンクから）
            url = ""
            status_links = card.locator('a[href*="/status/"]')
            if status_links.count():
                href = status_links.first.get_attribute("href") or ""
                if href.startswith("/"):
                    href = f"https://x.com{href}"
                # クエリパラメータ除去
                url = href.split("?")[0] if href else ""

            # 重複チェック
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)

            # 著者
            author = ""
            # URLからスクリーンネーム抽出
            if url and "/status/" in url:
                parts = url.replace("https://x.com/", "").split("/")
                if parts:
                    author = f"@{parts[0]}"

            # 投稿日時
            created_at = ""
            time_el = card.locator("time")
            if time_el.count():
                created_at = time_el.first.get_attribute("datetime") or ""

            # 数値指標の計測は 2026-08-19 廃止（ヘッダの履歴参照・欠測=null のまま）

            # 長文判定
            is_long = len(text) > 280

            if text or url:
                results.append(Bookmark(
                    url=url,
                    text=text.strip(),
                    author=author,
                    is_long_form=is_long,
                    created_at=created_at,
                ))
        except Exception as exc:
            logger.debug("カード%d解析スキップ: %s", i, exc)

    return results


def _upsert_dom_bookmark(dom_bookmarks: Dict[str, Bookmark], bm: Bookmark) -> None:
    """DOM取得ブックマークを追加/更新する。テキスト空→非空の更新を許可。"""
    existing = dom_bookmarks.get(bm.url)
    if existing is None:
        dom_bookmarks[bm.url] = bm
    elif not existing.text and bm.text:
        dom_bookmarks[bm.url] = bm


# _parse_metric は 2026-08-19 に削除（計測廃止）。K/M しか扱えず日本語表示の
# 「万」で例外→0 を返し、3万いいねの投稿を 0 と保存する全損の原因だった
# （敵対レビュー A#1・実証4件）。数値が要る時は scripts/x_metrics_lib.py を使う。


# ── GraphQL 傍受（補助取得）──────────────────────────────────

def _extract_entries(data: dict) -> list:
    entries = []
    try:
        timeline = (
            data.get("data", {})
            .get("bookmark_timeline_v2", data.get("data", {}).get("bookmark_timeline", {}))
            .get("timeline", {})
            .get("instructions", [])
        )
        for instruction in timeline:
            if instruction.get("type") == "TimelineAddEntries":
                for entry in instruction.get("entries", []):
                    content = entry.get("content", {})
                    if content.get("entryType") == "TimelineTimelineItem":
                        item_content = content.get("itemContent", {})
                        if item_content.get("tweet_results", {}).get("result"):
                            entries.append(item_content["tweet_results"]["result"])
    except Exception as exc:
        logger.debug("エントリ抽出エラー: %s", exc)

    if not entries:
        entries = _deep_find_tweets(data)
    return entries


def _deep_find_tweets(obj, depth=0) -> list:
    results = []
    if depth > 10:
        return results
    if isinstance(obj, dict):
        if "tweet_results" in obj and isinstance(obj["tweet_results"], dict):
            result = obj["tweet_results"].get("result")
            if result:
                results.append(result)
        for v in obj.values():
            results.extend(_deep_find_tweets(v, depth + 1))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_deep_find_tweets(item, depth + 1))
    return results


def _parse_tweet_entry(entry: dict) -> Optional[Bookmark]:
    try:
        tweet = entry
        if "tweet" in entry:
            tweet = entry["tweet"]

        core = tweet.get("core", {}).get("user_results", {}).get("result", {})
        legacy = tweet.get("legacy", {})
        user_legacy = core.get("legacy", {})

        screen_name = user_legacy.get("screen_name", "")
        tweet_id = legacy.get("id_str", tweet.get("rest_id", ""))
        full_text = legacy.get("full_text", "")
        created_at = legacy.get("created_at", "")

        # 数値指標の取り込みは 2026-08-19 廃止（ヘッダの履歴参照・欠測=null のまま）。
        # GraphQL 傍受の役割は full_text / note_tweet（長文原文）の取得に限定する。
        is_long = len(full_text) > 280
        note_tweet = tweet.get("note_tweet", {}).get("note_tweet_results", {}).get("result", {})
        if note_tweet:
            full_text = note_tweet.get("text", full_text)
            is_long = True

        url = f"https://x.com/{screen_name}/status/{tweet_id}" if screen_name and tweet_id else ""

        return Bookmark(
            url=url, text=full_text, author=f"@{screen_name}",
            is_long_form=is_long, created_at=created_at,
        )
    except Exception as exc:
        logger.debug("ツイート解析エラー: %s", exc)
        return None


# ── テキスト空ブックマーク補完取得 ──────────────────────────────

def _backfill_empty_text(page, bookmarks: List[Bookmark], max_items: int = 30) -> int:
    """テキスト空のブックマークを個別ツイートページで補完取得する。

    ブックマークタイムライン上でArticleカード等の特殊レイアウトにより
    tweetText要素が存在しないケースを補完する。

    Returns:
        補完できた件数。
    """
    empty = [bm for bm in bookmarks if not bm.text and bm.url]
    if not empty:
        return 0

    targets = empty[:max_items]
    logger.info("テキスト空 %d件を個別ページから補完取得中...", len(targets))
    filled = 0

    for bm in targets:
        try:
            page.goto(bm.url, wait_until="domcontentloaded", timeout=12000)
            page.wait_for_timeout(3000)
            text_el = page.locator('[data-testid="tweetText"]')
            if text_el.count():
                try:
                    text = text_el.first.inner_text(timeout=3000) or ""
                except Exception:
                    text = text_el.first.text_content() or ""
                if text.strip():
                    bm.text = text.strip()
                    bm.is_long_form = len(bm.text) > 280
                    filled += 1
            time.sleep(random.uniform(0.5, 1.5))
        except Exception as exc:
            logger.debug("補完取得スキップ %s: %s", bm.url, exc)

    if filled:
        logger.info("  → %d/%d件のテキストを補完", filled, len(targets))
    return filled


# ── スクロール待機 ─────────────────────────────────────────────

def _wait_for_new_tweets(page, prev_count: int, timeout_sec: float = 5.0) -> int:
    """スクロール後、ツイートカード数が増えるのを最大timeout_sec秒待つ。

    Returns:
        現在のツイートカード数。増えなければprev_countと同じ値。
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        current = page.locator('[data-testid="tweet"]').count()
        if current > prev_count:
            return current
        time.sleep(0.5)
    return page.locator('[data-testid="tweet"]').count()


# ── メイン取得フロー ─────────────────────────────────────────

def fetch_bookmarks(
    profile_path: str = "./x_profile",
    max_scrolls: Optional[int] = None,
    max_empty_batches: int = 5,
    max_runtime_min: int = 30,
    headless: bool = False,
    checkpoint_path: Optional[str] = None,
    out_path: Optional[str] = None,
    status_json_path: Optional[str] = None,
    append: bool = False,
    persistent: bool = False,
) -> List[Bookmark]:
    from playwright.sync_api import sync_playwright

    if not persistent:
        cookies = load_cookies(profile_path)
    else:
        cookies = None

    # チェックポイント復元
    cp = load_checkpoint(checkpoint_path)
    seen_urls: set = cp["seen_urls"]

    # 逐次保存用ファイルハンドル準備
    out_file = None
    existing_records: List[dict] = []  # 追記モード時、終了時のマージ書き出しで使う既存全件
    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if append and p.exists():
            # 追記モード: 既存ファイルからレコード全件とseen_urlsを復元
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            d = json.loads(line)
                            existing_records.append(d)
                            if d.get("url"):
                                seen_urls.add(d["url"])
                logger.info("既存ファイルから%d件のURLを重複スキップ対象に追加", len(seen_urls))
            except Exception as exc:
                logger.warning("既存ファイル読込失敗: %s", exc)
            out_file = open(p, "a", encoding="utf-8")
        else:
            out_file = open(p, "w", encoding="utf-8")

    graphql_bookmarks: Dict[str, Bookmark] = {}  # url -> Bookmark
    dom_bookmarks: Dict[str, Bookmark] = {}
    all_bookmarks: List[Bookmark] = []  # 逐次保存済みリスト
    start_time = time.time()

    def _is_runtime_exceeded() -> bool:
        elapsed_min = (time.time() - start_time) / 60.0
        if elapsed_min >= max_runtime_min:
            logger.info("最大実行時間(%d分)に到達（経過: %.1f分）", max_runtime_min, elapsed_min)
            return True
        return False

    def _flush_new_bookmarks(scroll: int) -> None:
        """マージ結果から新規分をJSONLにappendし、チェックポイントを更新する。"""
        # GraphQL（より詳細）を優先し、DOM（確実）で補完してマージ
        merged: Dict[str, Bookmark] = {}
        for url, bm in dom_bookmarks.items():
            merged[url] = bm
        for url, bm in graphql_bookmarks.items():
            merged[url] = bm

        new_count = 0
        for url, bm in merged.items():
            if url not in seen_urls:
                seen_urls.add(url)
                all_bookmarks.append(bm)
                if out_file:
                    out_file.write(json.dumps(asdict(bm), ensure_ascii=False) + "\n")
                    out_file.flush()
                new_count += 1

        if new_count > 0:
            logger.info("  → %d件を新規保存（累計: %d件）", new_count, len(all_bookmarks))

        save_checkpoint(checkpoint_path, seen_urls, len(all_bookmarks), scroll)

    def handle_response(response):
        """context.on("response") ハンドラ — SW無効化時のGraphQL傍受。"""
        try:
            url = response.url
            if "Bookmarks" not in url and "bookmarks" not in url:
                return
            if "graphql" not in url and "/i/api/" not in url:
                return
            data = response.json()
            entries = _extract_entries(data)
            for entry in entries:
                bm = _parse_tweet_entry(entry)
                if bm and bm.url and bm.url not in graphql_bookmarks:
                    graphql_bookmarks[bm.url] = bm
                    logger.info("[GraphQL] @%s - %s...", bm.author, bm.text[:40])
        except Exception as exc:
            logger.debug("GraphQLレスポンス解析スキップ: %s", exc)

    pw = None
    browser = None
    context = None
    stopped_reason = "error"  # try内で正常な停止理由に更新されなければ「異常終了」扱い
    try:
        pw = sync_playwright().start()

        if persistent:
            # persistent context: ブラウザプロファイルのログイン状態を直接利用
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(Path(profile_path).resolve()),
                headless=headless,
                viewport={"width": 1280, "height": 900},
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
                args=["--disable-blink-features=AutomationControlled"],
            )
            context.on("response", handle_response)
            page = context.pages[0] if context.pages else context.new_page()
        else:
            browser = pw.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )

            # Service Worker を無効化して GraphQL を直接傍受
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
                service_workers="block",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            context.add_cookies(cookies)

            # context レベルでレスポンス監視（SW対応）
            context.on("response", handle_response)

            page = context.new_page()

        logger.info("ブックマークページにアクセス中...")
        page.goto("https://x.com/i/bookmarks", wait_until="domcontentloaded", timeout=60000)
        time.sleep(random.uniform(4.0, 6.0))

        if "/login" in page.url or "/i/flow/login" in page.url:
            raise CookieExpiredError.login_redirect(page.url)

        # ツイートカードの表示を待つ
        try:
            page.wait_for_selector('[data-testid="tweet"]', timeout=15000)
            logger.info("ブックマーク表示確認")
        except Exception:
            logger.warning("ブックマークが表示されません（0件の可能性）")
            stopped_reason = "end"
            return all_bookmarks

        # 初回DOM取得
        initial_dom = scrape_bookmarks_from_dom(page)
        for bm in initial_dom:
            if bm.url:
                _upsert_dom_bookmark(dom_bookmarks, bm)
        logger.info("初回DOM取得: %d件", len(initial_dom))

        # 初回分を逐次保存
        _flush_new_bookmarks(0)

        # スクロールしながら取得
        scroll_count = 0
        stale_count = 0

        while True:
            if max_scrolls is not None and scroll_count >= max_scrolls:
                logger.info("最大スクロール数(%d)に到達", max_scrolls)
                stopped_reason = "end"
                break

            if _is_runtime_exceeded():
                stopped_reason = "max_runtime"
                break

            # スクロール前のツイートカード数を記録
            prev_tweet_count = page.locator('[data-testid="tweet"]').count()

            page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
            scroll_count += 1

            # ツイートカード数が増えるのを最大5秒待つ
            new_tweet_count = _wait_for_new_tweets(page, prev_tweet_count, timeout_sec=5.0)

            # DOM からスクレイピング
            new_dom = scrape_bookmarks_from_dom(page)
            prev_dom_count = len(dom_bookmarks)
            prev_gql_count = len(graphql_bookmarks)
            for bm in new_dom:
                if bm.url:
                    _upsert_dom_bookmark(dom_bookmarks, bm)

            # 新規ブックマークを逐次保存
            _flush_new_bookmarks(scroll_count)

            # stale判定: DOMもGraphQLも増えていないか
            current_dom_count = len(dom_bookmarks)
            current_gql_count = len(graphql_bookmarks)
            if current_dom_count == prev_dom_count and current_gql_count == prev_gql_count:
                stale_count += 1
                if stale_count >= max_empty_batches:
                    logger.info("新しいブックマークなし（%d回連続）。取得完了。", max_empty_batches)
                    stopped_reason = "max_empty"
                    break
            else:
                stale_count = 0

            # レート制限チェック
            rate_limit = page.query_selector('text="Something went wrong"')
            if rate_limit:
                logger.warning("レート制限検出。60秒待機...")
                time.sleep(60)
                page.reload()
                time.sleep(5)

            logger.info(
                "スクロール %d: DOM=%d件 GraphQL=%d件 保存済み=%d件 (stale=%d/%d)",
                scroll_count, len(dom_bookmarks), len(graphql_bookmarks),
                len(all_bookmarks), stale_count, max_empty_batches,
            )

        # 最終フラッシュ（取りこぼし防止）
        _flush_new_bookmarks(scroll_count)

        # テキスト空ブックマークを個別ページから補完取得
        filled = _backfill_empty_text(page, all_bookmarks)

        # 出力ファイルの最終書き出し
        if append and out_path:
            # 追記モード: 既存全件 ∪ 今回取得分を原子的に書き出す（データ消失防止・件数不減少ガード）
            if out_file:
                out_file.close()
                out_file = None
            fetched_records = [asdict(bm) for bm in all_bookmarks]
            merged_records = merge_bookmark_records(existing_records, fetched_records)
            atomic_write_jsonl(out_path, merged_records, min_count=len(existing_records))
            logger.info(
                "出力ファイルを原子的に更新（既存%d件 + 今回取得%d件 → 合計%d件、テキスト補完%d件反映）",
                len(existing_records), len(fetched_records), len(merged_records), filled,
            )
        elif filled and out_path:
            # 上書きモード（--appendなし）: 従来どおり今回取得分のみを書き直す
            try:
                if out_file:
                    out_file.close()
                    out_file = None
                with open(out_path, "w", encoding="utf-8") as f:
                    for bm in all_bookmarks:
                        f.write(json.dumps(asdict(bm), ensure_ascii=False) + "\n")
                logger.info("出力ファイルを更新済み（テキスト補完%d件反映）", filled)
            except Exception as exc:
                logger.warning("出力ファイル再書き込み失敗: %s", exc)

        elapsed = (time.time() - start_time) / 60.0
        logger.info("最終結果: DOM=%d, GraphQL=%d, 保存済み=%d (%.1f分)",
                     len(dom_bookmarks), len(graphql_bookmarks), len(all_bookmarks), elapsed)

        return all_bookmarks

    except Exception:
        stopped_reason = "error"
        raise

    finally:
        if status_json_path:
            _write_status_json(status_json_path, dom_bookmarks, graphql_bookmarks, all_bookmarks, stopped_reason)
        if out_file:
            try:
                out_file.close()
            except Exception:
                pass
        if context:
            try:
                context.close()
            except Exception:
                pass
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Xのブックマーク一覧を取得する")
    parser.add_argument("--out", required=True, help="出力JSONLファイルパス")
    parser.add_argument("--max-scrolls", type=int, default=None, help="最大スクロール回数（デフォルト: 無制限）")
    parser.add_argument("--max-empty-batches", type=int, default=5, help="N回連続で新規0件なら停止（デフォルト: 5）")
    parser.add_argument("--max-runtime-min", type=int, default=30, help="最大実行時間（分、デフォルト: 30）")
    parser.add_argument("--checkpoint", default=None, help="チェックポイントJSONパス")
    parser.add_argument(
        "--status-json", default=None,
        help="実行結果サマリ(saved/dom_count/graphql_count/stopped_reason)の出力先JSONパス",
    )
    parser.add_argument("--append", action="store_true", help="既存ファイルに追記")
    parser.add_argument("--headless", action="store_true", help="ヘッドレスモード")
    parser.add_argument("--persistent", action="store_true", help="persistent contextモード（ブラウザプロファイルのログイン状態を直接利用）")
    parser.add_argument("--profile", default="./x_profile", help="Cookieプロファイルパス")
    args = parser.parse_args()

    try:
        bookmarks = fetch_bookmarks(
            profile_path=args.profile,
            max_scrolls=args.max_scrolls,
            max_empty_batches=args.max_empty_batches,
            max_runtime_min=args.max_runtime_min,
            headless=args.headless,
            checkpoint_path=args.checkpoint,
            out_path=args.out,
            status_json_path=args.status_json,
            append=args.append,
            persistent=args.persistent,
        )

        long_form = sum(1 for b in bookmarks if b.is_long_form)
        print(f"\n=== ブックマーク取得完了 ===")
        print(f"合計: {len(bookmarks)}件")
        print(f"長文: {long_form}件 / 短文: {len(bookmarks) - long_form}件")
        print(f"出力: {args.out}")
        if args.checkpoint:
            print(f"チェックポイント: {args.checkpoint}")

    except CookieExpiredError as e:
        logger.error("Cookie期限切れ: %s", e)
        print("\nCookieが期限切れです。以下を実行してください:")
        print("  python3 scripts/import_chrome_cookies.py --chrome-profile \"<Chrome profile>\" --account <your-account>")
        print("  （詳細: refresh-x-cookies スキル参照）")
        sys.exit(20)
    except Exception as e:
        logger.exception("予期しないエラー")
        sys.exit(1)


if __name__ == "__main__":
    main()
