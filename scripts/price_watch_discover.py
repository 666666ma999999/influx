"""price_watch_discover: 値上がり商品の「発見器」（新商品名の自動抽出→候補キュー）.

2AI敵対レビュー一致設計（2026-07-28）: 検索本数を増やさず、低閾値の汎用検索
（値上げ/値上がり/品薄・min_faves:20・前日1日窓）で集めた投稿本文から、
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

DISCOVER_QUERIES = ["値上げ", "値上がり", "品薄"]
MIN_FAVES = 20
PER_QUERY = 60
MAX_SCROLLS = 4
PROFILE = APP / "x_profiles/maaaki"
QUEUE_PATH = APP / "data/x_price_watch/discovery_queue.jsonl"
UNIVERSE_MD = APP / "docs/price-watch-universe.md"
CANDIDATE_CAP = 20

# 候補トークン: カタカナ3+ / 型番風ASCII(数字含む3+) / 漢字3+
TOKEN_RE = re.compile(
    r"[ァ-ヴー]{3,}|(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9][A-Za-z0-9\-]{2,}|[一-龠]{3,}"
)
PRICE_HINT_RE = re.compile(r"[0-9][0-9,.]*\s*(?:円|万円|ドル|%|％|倍)")
STOPLIST = {
    # 価格語・一般語（検索語自体と頻出ノイズ）
    "値上げ", "値上がり", "品薄", "高騰", "価格改定", "価格転嫁", "物価上昇", "インフレ",
    "ニュース", "サービス", "ポイント", "キャンペーン", "フォロー", "リポスト", "プレゼント",
    "アカウント", "チェック", "ランキング", "セール", "クーポン", "メンバー", "チャンネル",
    "スーパー", "コンビニ", "メーカー", "アメリカ", "トランプ", "ガソリン", "エネルギー",
    "こちら", "それぞれ", "みなさん", "ありがとう", "ポスト", "セット", "再入荷", "入荷", "予約", "発売",
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
            by_id[r["id"]] = r
    return list(by_id.values()), stats


def extract_candidates(posts: list[dict], known: set[str]) -> list[dict]:
    """ルールベース抽出: 既知語彙・ストップリストを除いた候補トークンを採点する。"""
    stats: dict[str, dict] = defaultdict(lambda: {"authors": set(), "posts": 0, "price_hint": 0,
                                                  "likes": 0, "sample": ""})
    for post in posts:
        text = unicodedata.normalize("NFKC", post.get("content") or "")
        if not text:
            continue
        has_price = bool(PRICE_HINT_RE.search(text))
        for token in set(TOKEN_RE.findall(text)):
            if token in known or token in STOPLIST or len(token) < 3:
                continue
            if re.fullmatch(r"[0-9,.\-]+", token):  # 数字だけの断片（金額の切れ端）は除外
                continue
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
        score = n_authors * (1 + s["price_hint"])
        candidates.append({
            "token": token, "score": score, "authors": n_authors, "posts": s["posts"],
            "price_hint_posts": s["price_hint"], "likes_sum": s["likes"], "sample": s["sample"],
        })
    candidates.sort(key=lambda c: -c["score"])
    return candidates[:CANDIDATE_CAP]


def llm_refine(candidates: list[dict]) -> list[dict] | None:
    """LLM精製段（キーが実キーの時のみ）。候補が商品/素材名かを判定して絞る。"""
    import os
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key.startswith("sk-ant-"):
        print("[llm] スキップ: ANTHROPIC_API_KEY が未設定/プレースホルダ"
              "（ルールベース候補のみ出力。有効化は ~/.zshrc に実キー設定）")
        return None
    import urllib.request
    from collector.llm_classifier import DEFAULT_LLM_CONFIG  # モデル名等の正本を流用
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", help="対象UTC日 YYYY-MM-DD（省略時=前UTC日）")
    args = parser.parse_args()
    day = args.date or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    posts, stats = collect_posts(day)
    print(f"[collect] day={day} posts={len(posts)} per_query={stats['per_query']} "
          f"wall={stats['login_wall']}")
    if not posts:
        print("[done] 投稿ゼロ（候補なし）")
        return 1 if stats["login_wall"] or stats["errors"] else 0

    known = load_known_vocab()
    candidates = extract_candidates(posts, known)
    refined = llm_refine(candidates)
    final = refined if refined is not None else candidates

    event = {
        "type": "candidate_batch", "date": day, "n_posts": len(posts),
        "llm_refined": refined is not None, "candidates": final,
    }
    append_event(QUEUE_PATH, event)
    print(f"[done] 候補 {len(final)} 件 → {QUEUE_PATH}")
    for c in final[:10]:
        print(f"  {c['token']:<16} score={c['score']} authors={c['authors']} "
              f"price_hint={c['price_hint_posts']}  例: {c['sample'][:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
