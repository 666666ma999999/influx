#!/usr/bin/env python3
"""Xブックマーク→キーワード抽出パイプライン: digest収集CLI（無料ブラウザ収集版・xai_sdk不要）。

設計正本: ~/.claude/docs/x-keywords-plan.md 末尾セクション
「Plan: x-keywords v2 — AI 検索代行ダイジェスト」§詳細設計v2/§アーキテクチャ（C1バッチ）。

bookmarks_keyword_digest_collect.py（xai_sdk/API課金版）の無料版ツイン。
ログイン状態の再現は、本番実績のあるfetch_bookmarks.pyの非persistent方式
（x_profiles/maaaki/cookies.jsonをfetch_bookmarks.load_cookies()で読み込み、
通常起動したbrowserのcontextへcontext.add_cookies()で注入。UA/locale/timezoneも
fetch_bookmarks.pyと同じ値を使う。ただしAccept-Languageヘッダーはfetch_bookmarks.pyには
無い明示設定をbuild_context_kwargs()で追加している。詳細は同関数のdocstring参照）を
そのまま踏襲する。launch_persistent_context
（プロファイルディレクトリを直接開く方式）は2026-07-13の実走診断でログイン壁
（「いま何が起こっているかチェック」画面へのリダイレクト）を再現したため不採用。
x.com/search をクラスタごとに代表クエリ1本（min_faves中央値のクエリをそのまま
使う・演算子付き）+ since:<days日前> を付与して開き、検索結果をDOMスクレイピングする。

出力契約はxai_sdk版と完全に同一: `<inbox>/digest_raw-<run_id>.jsonl`
（行スキーマ: url/id/author/content/likes/impressions/posted_at/cluster_id/seed_query_id）+
`<inbox>/digest_status-<run_id>.json`（run_id/source/generated_at/collect_days/
per_cluster_counts/errors/complete）。bookmarks_keyword_digest_apply.py（柵）・render_html・
worklist・ingestは無変更で流用できる（apply側はどちらの収集器が書いたraw/statusかを区別しない）。

xai_sdk版のpureヘルパー（select_min_likes_and_seed/normalize_item/build_status/
atomic_write_jsonl/atomic_write_json/load_latest_snapshot）をbookmarks_keyword_digest_collect
からそのままimportして再利用する（ロジック複製・ドリフトを避けるため。xai_sdk自体は
bookmarks_keyword_digest_collect.py内では遅延importのため、本スクリプトをimportしても
xai_sdkは不要）。playwrightはmain()実行時のみ遅延importする（host非依存を保つため。
grok_collect_twittora.py/bookmarks_keyword_digest_collect.pyの先例踏襲）。

クラスタ単位でtry/exceptするfail-soft方式。人間的ペーシングとしてクエリ間5〜10秒sleepし、
--max-runtime-min到達後の残りクラスタはper_cluster_counts=0として記録する（errorsには
入れない。complete=trueのまま。xai_sdk版と同じくexit 0で完走する設計）。

Docker実行（xstock-vncコンテナ・DISPLAY=:99必須）:
    docker exec -e DISPLAY=:99 xstock-vnc python scripts/bookmarks_keyword_digest_collect_browser.py \\
        --profile x_profiles/maaaki --run-id <id>

exit code: 0=完走（クラスタ単位のfail-softエラーはerrorsに記録の上0）/
    1=latest_snapshot.json不在（致命的セットアップ不備）/ 20=Cookie期限切れ
    （fetch_bookmarks.pyのCookieExpiredError exit codeと揃えた）
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # collectorパッケージ(project root)用
import bookmarks_keyword_digest_collect as digest_collect  # noqa: E402 (pureヘルパー再利用)
import fetch_bookmarks  # noqa: E402 (load_cookies()・非persistent context設定を再利用。playwright非依存)

# fetch_bookmarks.pyの非persistent contextと同じUA文字列（cookie発行時のブラウザ実体に
# 揃えることでフィンガープリント不一致による弾かれを避ける。値を変える場合は
# fetch_bookmarks.py側の同リテラルとの整合に注意すること）。
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

def build_context_kwargs(user_agent: str = None) -> dict:
    """browser.new_context()に渡すkwargsを組み立てる（playwright非依存・pureヘルパー）。

    locale="ja-JP"・timezone_id="Asia/Tokyo"に加え、Accept-Languageリクエストヘッダーを
    明示的に"ja"へ設定する（2026-07-13の実走で掲載記事の本文抜粋が英語に自動翻訳された
    事象を受けた対策。Playwrightはlocale指定からもAccept-Languageを連動させるはずだが、
    ヘッダーを明示することで設定意図を固定し、将来の変更による劣化を防ぐ）。
    """
    return {
        "viewport": {"width": 1280, "height": 900},
        "locale": "ja-JP",
        "timezone_id": "Asia/Tokyo",
        "service_workers": "block",
        "user_agent": user_agent if user_agent is not None else USER_AGENT,
        "extra_http_headers": {"Accept-Language": "ja"},
    }


DEFAULT_PROFILE = "x_profiles/maaaki"
DEFAULT_MAX_RUNTIME_MIN = 10
MAX_SCROLLS_PER_QUERY = 3
PACING_MIN_SEC = 5.0
PACING_MAX_SEC = 10.0
TWEET_CARD_SELECTOR = '[data-testid="tweet"]'

# 2026-07-13実走診断で観測したログイン壁の実際のリダイレクト先は
# `/i/jf/onboarding/web?...&mode=login`であり、`/login`・`/i/flow/login`という
# 単純な部分文字列では検知できなかった（selector待機タイムアウトとして黙って0件になる
# 不具合の直接原因）。既知のログイン壁パターンを広めに列挙する。
_LOGIN_WALL_MARKERS = ("/login", "mode=login", "/i/flow/login", "/i/jf/onboarding")


def is_login_wall_url(url: str) -> bool:
    """urlがXのログイン壁（ログイン画面・オンボーディング誘導）と判定できるかを返す。"""
    if not url:
        return False
    return any(marker in url.lower() for marker in _LOGIN_WALL_MARKERS)


class LoginWallError(Exception):
    """検索ページがログイン壁（is_login_wall_url()がTrue）にリダイレクトされた場合に送出する。"""


# --- pure ヘルパー（playwright非依存・host単体テスト可能） -------------------------

def select_representative_query(cluster: dict) -> tuple[str | None, str | None]:
    """クラスタの検索に使う代表クエリ(query_id, q文字列)を1つ選ぶ。

    xai_sdk版と同じdigest_collect.select_min_likes_and_seed()でmin_faves中央値の
    query_idを求め、対応するqueryのq(演算子付き文字列。lang:/min_faves:/-filter:等を
    そのまま含む)を返す。min_favesを持つクエリが1件も無い(seed_query_id=None)場合は
    queries[0]にフォールバックする。queriesが空なら(None, None)を返す
    （呼び出し側はValueErrorを送出しクラスタ単位のfail-softでerrorsに記録する）。
    """
    queries = cluster.get("queries", [])
    if not queries:
        return None, None
    _min_likes, seed_query_id = digest_collect.select_min_likes_and_seed(queries)
    if seed_query_id is not None:
        for q in queries:
            if q.get("query_id") == seed_query_id:
                return seed_query_id, q.get("q", "")
    first = queries[0]
    return first.get("query_id"), first.get("q", "")


def build_search_url(q: str, days: int, now: datetime | None = None) -> str:
    """qにsince:<days日前>(YYYY-MM-DD)を付与し、X検索URL(f=live)を組み立てる。

    qが既にsince:を含む場合は二重付与しない（呼び出し側のクエリ文字列を信頼しすぎない
    防御）。クエリ全体をurllib.parse.quoteでパーセントエンコードする。
    """
    now = now or datetime.now(timezone.utc)
    since_date = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    query = q if "since:" in q else f"{q} since:{since_date}"
    return f"https://x.com/search?q={quote(query)}&f=live"


def parse_likes_text(text: str) -> int:
    """likesの表示文字列をintへ変換する。取れなければ0。

    対応形式: "1,234"（桁区切りカンマ）/ "1.5万"・"2億"（日本語locale表記）/
    "12.3K"・"4.5M"（英語locale表記）/ 素の数字。空文字・"0"・変換失敗は0を返す。
    """
    if not text:
        return 0
    text = text.strip().replace(",", "")
    if not text:
        return 0
    try:
        if text.endswith("万"):
            return int(float(text[:-1]) * 10_000)
        if text.endswith("億"):
            return int(float(text[:-1]) * 100_000_000)
        if text and text[-1] in ("K", "k"):
            return int(float(text[:-1]) * 1_000)
        if text and text[-1] in ("M", "m"):
            return int(float(text[:-1]) * 1_000_000)
        return int(float(text))
    except ValueError:
        return 0


_STATUS_ID_RE = re.compile(r"/status/(\d+)")


def extract_author_from_url(url: str) -> str:
    """X投稿URLからhandle(@なし)を抽出する。取れなければ空文字。"""
    if not url or "/status/" not in url:
        return ""
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    return parts[0] if parts and parts[0] else ""


def build_candidate_from_raw(url: str, author: str, content: str, likes_text: str, datetime_attr: str) -> dict:
    """DOMから抽出した生の値を、digest_collect.normalize_item()に渡す候補dictへ正規化する
    （playwright非依存）。urlからstatus IDを抽出できなければid=""（呼び出し側でスキップ判断）。
    datetime_attrはtime要素のdatetime属性(ISO8601)を想定し、先頭10文字(YYYY-MM-DD)を
    posted_atとして採用する（xai_sdk版DigestCandidate.posted_atと同じ"YYYY-MM-DD"形式）。
    """
    m = _STATUS_ID_RE.search(url or "")
    post_id = m.group(1) if m else ""
    posted_at = datetime_attr[:10] if datetime_attr else ""
    return {
        "id": post_id,
        "url": url or "",
        "author": author or "",
        "content": content or "",
        "likes": parse_likes_text(likes_text),
        "impressions": 0,
        "posted_at": posted_at,
    }


# --- playwright依存部（この関数群の呼び出し時にのみplaywrightが必要） -----------------

def scrape_search_results_from_dom(page) -> list[dict]:
    """検索結果ページに表示中のtweetカードから候補dictのリストを抽出する。

    fetch_bookmarks.pyのscrape_bookmarks_from_dom()のDOM抽出方針（tweet要素・
    tweetText・status linkからのURL・time要素のdatetime属性）を検索結果ページ向けに
    援用する（fetch_bookmarks.py自体は変更しない・別実装として本関数に持つ）。
    """
    results = []
    cards = page.locator(TWEET_CARD_SELECTOR)
    count = cards.count()

    for i in range(count):
        try:
            card = cards.nth(i)

            content = ""
            text_el = card.locator('[data-testid="tweetText"]')
            if text_el.count():
                try:
                    content = text_el.first.inner_text(timeout=3000) or ""
                except Exception:
                    content = text_el.first.text_content() or ""

            url = ""
            status_links = card.locator('a[href*="/status/"]')
            if status_links.count():
                href = status_links.first.get_attribute("href") or ""
                if href.startswith("/"):
                    href = f"https://x.com{href}"
                url = href.split("?")[0] if href else ""

            if not url:
                continue

            author = extract_author_from_url(url)

            datetime_attr = ""
            time_el = card.locator("time")
            if time_el.count():
                datetime_attr = time_el.first.get_attribute("datetime") or ""

            likes_text = ""
            like_el = card.locator('[data-testid="like"]')
            if like_el.count():
                likes_text = like_el.first.text_content() or ""

            results.append(build_candidate_from_raw(url, author, content.strip(), likes_text, datetime_attr))
        except Exception:
            continue  # カード単位のfail-soft（1件の解析失敗で検索結果全体を捨てない）

    return results


def _wait_for_new_cards(page, prev_count: int, timeout_sec: float = 5.0) -> int:
    """スクロール後、tweetカード数が増えるのを最大timeout_sec秒待つ。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        current = page.locator(TWEET_CARD_SELECTOR).count()
        if current > prev_count:
            return current
        time.sleep(0.5)
    return page.locator(TWEET_CARD_SELECTOR).count()


_DEBUG_SELECTORS = {
    "tweet": TWEET_CARD_SELECTOR,
    "tweetText": '[data-testid="tweetText"]',
    "cellInnerDiv": '[data-testid="cellInnerDiv"]',
    "like": '[data-testid="like"]',
    "time": "time",
    "primaryColumn": '[data-testid="primaryColumn"]',
    "empty_state": '[data-testid="empty_state_header_text"]',
    "something_wrong": 'text="Something went wrong"',
}


def _debug_snapshot(page, inbox: Path, cluster_id: str, phase: str) -> None:
    """--debug時のみ呼ばれる診断出力（原因特定: ログイン壁/『再試行』画面/selector不一致/
    待機不足の切り分け用）。実台帳・inbox契約ファイル(digest_raw/digest_status)には
    一切触れず、debug_<cluster_id>_<phase>.pngと標準出力へのログのみを行う。
    """
    try:
        inbox.mkdir(parents=True, exist_ok=True)
        png_path = inbox / f"debug_{cluster_id}_{phase}.png"
        page.screenshot(path=str(png_path))
        print(f"    [debug] screenshot: {png_path}")
    except Exception as exc:
        print(f"    [debug] screenshot failed: {exc}", file=sys.stderr)

    counts = {}
    for name, sel in _DEBUG_SELECTORS.items():
        try:
            counts[name] = page.locator(sel).count()
        except Exception:
            counts[name] = -1
    try:
        title = page.title()
    except Exception:
        title = "<title取得失敗>"
    print(f"    [debug] phase={phase} url={page.url}")
    print(f"    [debug] title={title!r}")
    print(f"    [debug] selector hits: {counts}")


def collect_for_cluster(
    page, cluster: dict, days: int, max_per_cluster: int,
    debug: bool = False, inbox: Path | None = None,
) -> list[dict]:
    """1クラスタ分を検索ページから収集し、digest_raw.jsonlの行スキーマに正規化して返す。

    スクロールは最大MAX_SCROLLS_PER_QUERY回・カード数が新規に増えなければ打ち切り。
    ログイン壁（/login・/i/flow/login へのリダイレクト）はLoginWallErrorを送出する
    （呼び出し側でクラスタ単位のfail-softとしてerrorsに記録する契約）。検索結果が
    0件（selector非表示・ログイン壁ではない）は空リストを返す（正常系）。
    debug=True（+inbox指定）の場合のみ、ページ遷移直後とselector待機タイムアウト時に
    _debug_snapshot()で診断情報を書き出す（out契約ファイルには影響しない）。
    """
    cid = cluster.get("cluster_id")
    query_id, q = select_representative_query(cluster)
    if not q:
        raise ValueError(f"cluster {cid!r} has no queries (q なし)")

    url = build_search_url(q, days)
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(random.uniform(3.0, 5.0))

    if debug and inbox is not None:
        _debug_snapshot(page, inbox, cid, "after_goto")

    if is_login_wall_url(page.url):
        raise LoginWallError(f"ログイン壁にリダイレクト: {page.url}")

    try:
        page.wait_for_selector(TWEET_CARD_SELECTOR, timeout=10000)
    except Exception:
        if debug and inbox is not None:
            _debug_snapshot(page, inbox, cid, "wait_timeout")
        return []  # 検索結果0件（ログイン壁は上でraise済み）

    prev_seen = 0
    for _ in range(MAX_SCROLLS_PER_QUERY):
        current_count = page.locator(TWEET_CARD_SELECTOR).count()
        if current_count >= max_per_cluster:
            break
        page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        new_count = _wait_for_new_cards(page, current_count)
        if new_count <= prev_seen and new_count <= current_count:
            break  # 新規ゼロで打ち切り
        prev_seen = current_count

    candidates = scrape_search_results_from_dom(page)
    seen_urls: set = set()
    deduped: list = []
    for c in candidates:
        if c["url"] and c["url"] not in seen_urls:
            seen_urls.add(c["url"])
            deduped.append(c)

    return [
        digest_collect.normalize_item(c, cid, query_id)
        for c in deduped[:max_per_cluster]
    ]


# --- CLI本体 -------------------------------------------------------------------

def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inbox", default=digest_collect.DEFAULT_INBOX)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--days", type=int, default=digest_collect.DEFAULT_DAYS)
    ap.add_argument("--max-per-cluster", type=int, default=digest_collect.DEFAULT_MAX_PER_CLUSTER)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--max-runtime-min", type=int, default=DEFAULT_MAX_RUNTIME_MIN)
    ap.add_argument(
        "--debug", action="store_true",
        help="診断モード: 各クラスタの検索直後/selector待機タイムアウト時にスクリーンショット"
             "(<inbox>/debug_<cluster_id>_<phase>.png)とselectorヒット数を標準出力へログする"
             "(inbox契約ファイル=digest_raw/digest_statusには影響しない)",
    )
    ap.add_argument(
        "--max-clusters", type=int, default=None,
        help="処理するクラスタ数の上限(診断実行を安価に絞るためのデバッグ用オプション。"
             "未指定なら全active_clustersを処理)",
    )
    return ap.parse_args()


def main() -> int:
    args = parse_args()

    inbox = Path(args.inbox)
    run_id = args.run_id or uuid.uuid4().hex

    try:
        snapshot = digest_collect.load_latest_snapshot(inbox)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    from collector.exceptions import CookieExpiredError

    try:
        cookies = fetch_bookmarks.load_cookies(args.profile)
    except CookieExpiredError as exc:
        print(f"ERROR: Cookie期限切れ: {exc}", file=sys.stderr)
        return 20

    active_clusters = snapshot.get("active_clusters", [])
    if args.max_clusters is not None:
        active_clusters = active_clusters[: args.max_clusters]
    print(f"=== bookmarks_keyword_digest_collect_browser run_id={run_id} ===")
    print(f"active_clusters: {len(active_clusters)}, days: {args.days}, max_per_cluster: {args.max_per_cluster}")

    from playwright.sync_api import sync_playwright  # 遅延import（host非依存を保つため）

    rows: list[dict] = []
    per_cluster_counts: dict[str, int] = {}
    errors: list[dict] = []
    start_time = time.monotonic()

    def runtime_exceeded() -> bool:
        return (time.monotonic() - start_time) / 60.0 >= args.max_runtime_min

    pw = None
    browser = None
    context = None
    try:
        pw = sync_playwright().start()
        # fetch_bookmarks.fetch_bookmarks()の非persistentブランチと同じ構成
        # （通常起動browser + cookie注入）。launch_persistent_context(profileディレクトリを
        # 直接開く方式)は2026-07-13の実走診断でログイン壁を再現したため不採用（モジュール
        # docstring参照）。
        browser = pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(**build_context_kwargs())
        context.add_cookies(cookies)
        page = context.new_page()

        for cluster in active_clusters:
            cid = cluster.get("cluster_id")

            if runtime_exceeded():
                print(f"  ⏱ 最大実行時間({args.max_runtime_min}分)到達。以降のクラスタをスキップ: {cluster.get('name', cid)!r}")
                per_cluster_counts[cid] = 0
                continue

            print(f"  → collecting: {cluster.get('name', cid)!r}")
            try:
                items = collect_for_cluster(
                    page, cluster, args.days, args.max_per_cluster,
                    debug=args.debug, inbox=inbox,
                )
            except Exception as exc:
                print(f"    ✗ 失敗: {exc}", file=sys.stderr)
                errors.append({"cluster_id": cid, "error": str(exc)})
                per_cluster_counts[cid] = 0
                continue

            print(f"    got {len(items)} candidates")
            rows.extend(items)
            per_cluster_counts[cid] = len(items)
            time.sleep(random.uniform(PACING_MIN_SEC, PACING_MAX_SEC))
    finally:
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

    generated_at = datetime.now(timezone.utc).isoformat()
    status = digest_collect.build_status(
        run_id=run_id,
        source={
            "generation": snapshot.get("generation"),
            "revision": snapshot.get("revision"),
            "urls_sha256": (snapshot.get("source_snapshot") or {}).get("urls_sha256"),
        },
        per_cluster_counts=per_cluster_counts,
        errors=errors,
        generated_at=generated_at,
        collect_days=args.days,
        complete=True,
    )

    raw_path = inbox / f"digest_raw-{run_id}.jsonl"
    status_path = inbox / f"digest_status-{run_id}.json"
    digest_collect.atomic_write_jsonl(raw_path, rows)
    digest_collect.atomic_write_json(status_path, status)

    print(f"\n✓ {raw_path} ({len(rows)} entries)")
    print(f"✓ {status_path} (errors={len(errors)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
