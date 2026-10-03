"""price_watch_discover: 値上がり商品の「発見器」（新商品名の自動抽出→候補キュー）.

2AI敵対レビュー一致設計（2026-07-28）: 検索本数を増やさず、低閾値の汎用検索
（値上げ/値上がり/品薄＋2026-10-03 追加の品不足/入手困難/供給不足/逼迫・調達難は同日の便 3 で除外・min_faves:20・1日窓を `--days N` で過去 N 日ぶん 1 日ずつ回す〔runner は 7〕）で集めた投稿本文から、
ユニバース台帳に無い商品名候補をルールベース抽出し、採点して候補キューへ積む。

- 候補は**通知しない**（週次レビュー用のキュー。売買判断には5チェック必須）
- LLM精製段は ANTHROPIC_API_KEY が実キー（sk-ant-）の時のみ動作・プレースホルダなら明示スキップ
- 出力: data/x_price_watch/discovery_queue.jsonl（append-only・
  bookmarks_keyword_common.append_event 流用＝flock+fsync・type=candidate_batch）

実行（VNCコンテナ・週1〜隔日想定）:
    docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/price_watch_discover.py
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))

import fetch_bookmarks  # noqa: E402  Canonical cookie loader
import x_search_collect_twittora as xs  # noqa: E402  Canonical 検索収集
from bookmarks_keyword_common import append_event  # noqa: E402  Canonical 台帳append
from bookmarks_keyword_digest_collect_browser import (  # noqa: E402
    LoginWallError,
    build_context_kwargs,
)

# 入口語（主題を含まない汎用語＝固定クエリには足さず発見器の入口にだけ足す・P-INF-22 裁定 2026-10-03 Q1=a）
# 初期 3 語 + 2026-10-03 追加 5 語（品薄の言い換え 4＋作り手側の語 1・まず 3〜5 語の裁定どおり）
DISCOVER_QUERIES = ["値上げ", "値上がり", "品薄", "品不足", "入手困難", "供給不足", "逼迫"]  # 調達難は 2026-10-03 便 3 で除外（8 日 8 件・当たり 0）
MIN_FAVES = 20
PER_QUERY = 60
MAX_SCROLLS = 4
PROFILE = APP / "x_profiles/maaaki"
QUEUE_PATH = APP / "data/x_price_watch/discovery_queue.jsonl"
UNIVERSE_MD = APP / "docs/price-watch-universe.md"
CANDIDATE_CAP = 20

# 候補トークン: カタカナ3+ / 型番風ASCII(数字含む3+) / 漢字3+
TOKEN_RE = re.compile(
    r"[ァ-ヴー]{3,}|(?=[A-Za-z0-9-]*[0-9])[A-Za-z0-9][A-Za-z0-9\-]{2,}|[一-龠]{3,}"
)
PRICE_HINT_RE = re.compile(r"[0-9][0-9,.]*\s*(?:円|万円|ドル|%|％|倍)")
# 株式相場の実況投稿は「値上がり」クエリに大量ヒットするが、実物価格の話ではないので
# 投稿ごと除外する（実測: 日経平均まとめ1本から「日経平均/半導体関連株/全面高/P500」が候補化・B-3後続）
MARKET_POST_RE = re.compile(
    r"日経平均|大引け|寄り付き|前場|後場|終値|株式市場|全面高|全面安|東証|ダウ平均|"
    r"S&P|ナスダック|グロース市場|プライム市場|ストップ高|出来高|決算発表"
)
STOPLIST = {
    # 価格語・一般語（検索語自体と頻出ノイズ）
    "値上げ", "値上がり", "品薄", "高騰", "価格改定", "価格転嫁", "物価上昇", "インフレ",
    "品不足", "入手困難", "供給不足", "調達難",  # 2026-10-03 追加の入口語（検索語自体を候補化しない）
    "ニュース", "サービス", "ポイント", "キャンペーン", "フォロー", "リポスト", "プレゼント",
    "アカウント", "チェック", "ランキング", "セール", "クーポン", "メンバー", "チャンネル",
    "スーパー", "コンビニ", "メーカー", "アメリカ", "トランプ", "ガソリン", "エネルギー",
    "こちら", "それぞれ", "みなさん", "ありがとう", "ポスト", "セット", "再入荷", "入荷", "予約", "発売",
    # 2026-10-03 便 3（P-INF-22 A）: 品薄・入手困難で いいね 20 以上の主流だった消費財の告知語（グッズ 7・限定 12・チケット 6 等）
    "グッズ", "限定", "数量限定", "チケット", "カラー", "サイズ", "コーナー", "ゲット", "オススメ", "おすすめ",
}


def load_known_vocab() -> set[str]:
    """ユニバース台帳と監視クエリの既知語彙（=新規でない語）を集める。"""
    vocab: set[str] = set()
    for path in (UNIVERSE_MD, APP / "configs/x_price_watch.json"):
        if path.exists():
            text = path.read_text()
            vocab |= set(TOKEN_RE.findall(text))
    return {unicodedata.normalize("NFKC", v) for v in vocab}


def collect_posts(day: str) -> tuple[list[dict], dict]:
    """低閾値の汎用3クエリを1日窓で収集（_tmp_shortage_sweep と同配線・canonical流用）。"""
    next_day = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    xs.MAX_SCROLLS = MAX_SCROLLS
    stats: dict = {"per_query": {}, "login_wall": False, "errors": []}
    all_rows: list[dict] = []
    cookies = fetch_bookmarks.load_cookies(str(PROFILE))

    from playwright.sync_api import sync_playwright

    pw = browser = context = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=False, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(**build_context_kwargs())
        context.add_cookies(cookies)
        page = context.new_page()
        for i, q in enumerate(DISCOVER_QUERIES):
            try:
                rows = xs.collect_query(page, q, day, next_day, MIN_FAVES, PER_QUERY, lang="ja")
            except LoginWallError as exc:
                print(f"✗ ログイン壁で全体中断: {exc}", file=sys.stderr)
                stats["login_wall"] = True
                break
            except Exception as exc:  # noqa: BLE001  クエリ単位fail-soft
                stats["errors"].append(f"{q}: {type(exc).__name__}")
                time.sleep(random.uniform(5.0, 10.0))
                continue
            stats["per_query"][q] = len(rows)
            all_rows.extend(rows)
            if i < len(DISCOVER_QUERIES) - 1:
                time.sleep(random.uniform(20.0, 40.0))
    finally:
        for closer in (context, browser):
            try:
                closer and closer.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            pw and pw.stop()
        except Exception:  # noqa: BLE001
            pass

    by_id: dict[str, dict] = {}
    for r in all_rows:
        prev = by_id.get(r["id"])
        if prev is None or r["likes"] > prev["likes"]:
            if prev and prev.get("content") and not r.get("content"):
                r["content"] = prev["content"]  # 本文ありを空本文で潰さない（Codex CONFIRMED-3）
            by_id[r["id"]] = r
    return list(by_id.values()), stats


def extract_candidates(posts: list[dict], known: set[str]) -> list[dict]:
    """ルールベース抽出: 既知語彙・ストップリストを除いた候補トークンを採点する。"""
    stats: dict[str, dict] = defaultdict(lambda: {"authors": set(), "posts": 0, "price_hint": 0,
                                                  "likes": 0, "sample": ""})
    n_market_skipped = 0
    for post in posts:
        text = unicodedata.normalize("NFKC", post.get("content") or "")
        if not text:
            continue
        if MARKET_POST_RE.search(text):
            n_market_skipped += 1
            continue  # 株式相場の実況は実物価格の話ではない
        for token in set(TOKEN_RE.findall(text)):
            if token in known or token in STOPLIST or len(token) < 3:
                continue
            if re.fullmatch(r"[0-9,.\-]+", token):  # 数字だけの断片（金額の切れ端）は除外
                continue
            # 価格表現は候補語の前後15文字窓だけを見る（30文字だと隣接する無関係な相場値を
            # 拾う実測あり: 「日経平均 64,931円 …イラン」→ 15文字で解消・B-3）
            has_price = False
            for m in re.finditer(re.escape(token), text):
                window = text[max(0, m.start() - 15):m.end() + 15]
                if PRICE_HINT_RE.search(window):
                    has_price = True
                    break
            s = stats[token]
            s["authors"].add(post.get("author", ""))
            s["posts"] += 1
            s["price_hint"] += int(has_price)
            s["likes"] += post.get("likes", 0)
            if not s["sample"]:
                s["sample"] = " ".join(text.split())[:90]

    candidates = []
    for token, s in stats.items():
        n_authors = len(s["authors"])
        if n_authors < 2:  # 複数投稿者が最低条件（単発バズ・宣伝の除外）
            continue
        # 連投で青天井にならないよう比率化（寄与は最大2倍）・同点は likes でタイブレーク（B-3）
        hint_ratio = s["price_hint"] / max(s["posts"], 1)
        score = round(n_authors * (1 + hint_ratio), 2)
        candidates.append({
            "token": token, "score": score, "authors": n_authors, "posts": s["posts"],
            "price_hint_posts": s["price_hint"], "likes_sum": s["likes"], "sample": s["sample"],
        })
    candidates.sort(key=lambda c: (-c["score"], -c["authors"], -c["likes_sum"]))
    if n_market_skipped:
        print(f"[filter] 株式相場の実況投稿を {n_market_skipped} 件除外")
    return candidates[:CANDIDATE_CAP]


def llm_refine(candidates: list[dict]) -> list[dict] | None:
    """LLM精製段（キーが実キーの時のみ）。候補が商品/素材名かを判定して絞る。"""
    import os
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key.startswith("sk-ant-"):
        print("[llm] スキップ: ANTHROPIC_API_KEY が未設定/プレースホルダ"
              "（ルールベース候補のみ出力。有効化は ~/.zshrc に実キー設定）")
        return None
    try:
        import urllib.request
        from collector.llm_classifier import DEFAULT_LLM_CONFIG  # モデル名等の正本を流用
    except Exception as exc:  # noqa: BLE001  import失敗もルール結果へフォールバック（Codex CONFIRMED-5）
        print(f"[llm] import失敗（ルール結果を使用）: {str(exc)[:80]}")
        return None
    prompt = (
        "以下はX投稿から抽出した『値上がり/品薄の話題に出た語』の候補です。"
        "各語が【具体的な商品・素材・部材の名前】なら keep、一般語・サービス名・"
        "人名・イベント名・ミーム語なら drop と判定し、JSON配列 "
        '[{"token":"...","verdict":"keep|drop","category":"分野"}] だけを返してください。\n'
        + json.dumps([{"token": c["token"], "sample": c["sample"]} for c in candidates],
                     ensure_ascii=False)
    )
    body = json.dumps({
        "model": DEFAULT_LLM_CONFIG.get("model", "claude-3-5-haiku-20241022"),
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"X-API-Key": key, "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        text = data["content"][0]["text"]
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        verdicts = {v["token"]: v for v in json.loads(text)}
        refined = []
        for c in candidates:
            v = verdicts.get(c["token"], {})
            if v.get("verdict") == "keep":
                refined.append({**c, "category": v.get("category", "")})
        return refined
    except Exception as exc:  # noqa: BLE001  LLM失敗はルール結果へフォールバック
        print(f"[llm] 失敗（ルール結果を使用）: {str(exc)[:100]}")
        return None


def recorded_days(queue_path: Path) -> set[str]:
    """候補キューに「取れた」記録が既にある日（status ok/partial/empty）。error/login_wall の日は再試行対象。"""
    done: set[str] = set()
    if not queue_path.exists():
        return done
    for line in queue_path.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "candidate_batch" and ev.get("status") in ("ok", "partial", "empty"):
            done.add(ev.get("date", ""))
    return done


def window_days(end_day: str, days: int) -> list[str]:
    """end_day を最新として過去 days 日分の UTC 日を古い順に返す（days=1 なら [end_day]）。"""
    end = datetime.strptime(end_day, "%Y-%m-%d")
    return [(end - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]


def run_day(day: str, seen_ids: set[str] | None = None) -> int:
    """1 日分を収集→候補抽出→台帳 1 行。戻り値 0=成功／1=失敗（login_wall・errors）。

    seen_ids: 同じ実行の前の日で既に見た投稿 id。X の since/until は実測で約 2 日ぶん（JST 基準で
    since 日の 0 時〜until 日の終わり）を返すため、連日の窓は重なる＝同じ投稿を 2 日で二重に数えない
    （2026-10-03 実測: date=10-01 の保存 1,089 行のうち posted_at が 10-02 のもの 640 行）。
    """
    posts, stats = collect_posts(day)
    if seen_ids is not None:
        before = len(posts)
        posts = [r for r in posts if r.get("id") not in seen_ids]
        seen_ids.update(r.get("id") for r in posts if r.get("id"))
        if before != len(posts):
            print(f"[dedup] day={day} 前日までに見た投稿 {before - len(posts)} 件を除外")
    print(f"[collect] day={day} posts={len(posts)} per_query={stats['per_query']} "
          f"wall={stats['login_wall']}")
    if not posts:
        # 失敗週・ゼロ週も必ず1行残す（行なしだと実行忘れと区別できない・B-2）
        append_event(QUEUE_PATH, {
            "type": "candidate_batch", "date": day, "n_posts": 0,
            "status": "login_wall" if stats["login_wall"] else ("error" if stats["errors"] else "empty"),
            "per_query": stats["per_query"], "errors": stats["errors"],
            "llm_refined": False, "candidates": [],
        })
        print("[done] 投稿ゼロ（候補なし・台帳へ status 行を記録）")
        return 1 if stats["login_wall"] or stats["errors"] else 0

    known = load_known_vocab()
    candidates = extract_candidates(posts, known)
    refined = llm_refine(candidates)
    final = refined if refined is not None else candidates

    event = {
        "type": "candidate_batch", "date": day, "n_posts": len(posts),
        "status": "partial" if stats["errors"] else "ok",
        "per_query": stats["per_query"], "errors": stats["errors"],
        "llm_refined": refined is not None, "candidates": final,
    }
    append_event(QUEUE_PATH, event)
    print(f"[done] 候補 {len(final)} 件 → {QUEUE_PATH}")
    for c in final[:10]:
        print(f"  {c['token']:<16} score={c['score']} authors={c['authors']} "
              f"price_hint={c['price_hint_posts']}  例: {c['sample'][:60]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", help="対象UTC日 YYYY-MM-DD（省略時=前UTC日）。--days>1 ならこの日を最新とする")
    parser.add_argument("--days", type=int, default=1,
                        help="過去 N UTC 日を 1 日ずつ回す（既定 1＝前日のみ）。週1回の実行で 7 日分を見るための窓"
                             "（P-INF-22 裁定 2026-10-03 Q2=a: 週1回・1日分では 7 日中 6 日が未観測だった）")
    parser.add_argument("--force", action="store_true", help="既に ok/partial/empty で記録済みの日も取り直す")
    args = parser.parse_args()
    end_day = args.date or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    days = window_days(end_day, max(1, args.days))
    skip = set() if args.force else recorded_days(QUEUE_PATH)

    rc = 0
    ran = 0
    seen_ids: set[str] = set()
    for i, day in enumerate(days):
        if day in skip:
            print(f"[skip] day={day} は記録済み（--force で取り直し）")
            continue
        if ran > 0:
            time.sleep(random.uniform(30.0, 60.0))  # 日と日の間も検索間隔を空ける（連続アクセスを避ける）
        ran += 1
        try:
            r = run_day(day, seen_ids)
        except LoginWallError as exc:
            print(f"✗ ログイン壁で以降の日を中断: {exc}", file=sys.stderr)
            return 1
        rc = max(rc, r)
    print(f"[window] days={len(days)} ran={ran} skipped={len(days) - ran} rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
