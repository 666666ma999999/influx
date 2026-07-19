#!/usr/bin/env python3
"""@twittora_ 向けバズ投稿収集 — X ログイン検索スクレイプ版（2026-07-19 新設）。

背景: Grok x_search 版（grok_collect_twittora.py）が xAI クレジット枯渇で停止
（2026-07-01 確定判断: X 検索収集の第一選択は influx Cookie 自動収集・Grok は次点）。
本スクリプトはその方針の実装 — 既存部品（bookmarks_keyword_digest_collect_browser の
DOM 収集・ログイン壁検知・fetch_bookmarks の Cookie 注入）を再利用した薄いアダプタ。

出力契約は grok 版と同一: output/grok_twittora/grok-twittora-YYYY-MM-DD.jsonl + .md
（impressions は検索 DOM から取れないため 0 固定・collector フィールドで区別）。

実行（xstock-vnc コンテナ内・DISPLAY 必須）:
  docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/x_search_collect_twittora.py \
      --since 2026-06-22 --per-query 12
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fetch_bookmarks  # Cookie loader（canonical）
from bookmarks_keyword_digest_collect_browser import (  # DOM 部品を再利用
    LoginWallError,
    _wait_for_new_cards,
    build_context_kwargs,
    is_login_wall_url,
    scrape_search_results_from_dom,
)
from grok_collect_twittora import DEFAULT_MIN_LIKES, DEFAULT_QUERIES  # クエリ正本を共有

JST = timezone(timedelta(hours=9))
TWEET_CARD_SELECTOR = '[data-testid="tweet"]'
MAX_SCROLLS = 5
QUERY_PACING = (20.0, 40.0)  # クエリ間の待機（アカウント保護・秒）


def build_buzz_search_url(q: str, since: str, until: str, min_likes: int) -> str:
    """バズ検索 URL（f=top・min_faves・期間窓つき）。"""
    query = f"{q} min_faves:{min_likes} since:{since} until:{until} -filter:nativeretweets"
    return f"https://x.com/search?q={quote(query)}"  # f 省略 = top（高反応順）


def collect_query(page, q: str, since: str, until: str, min_likes: int, per_query: int) -> list[dict]:
    url = build_buzz_search_url(q, since, until, min_likes)
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    time.sleep(random.uniform(3.0, 5.0))
    if is_login_wall_url(page.url):
        raise LoginWallError(f"ログイン壁: {page.url}")
    try:
        page.wait_for_selector(TWEET_CARD_SELECTOR, timeout=10_000)
    except Exception:
        return []  # 0件（正常系）
    prev = 0
    for _ in range(MAX_SCROLLS):
        cur = page.locator(TWEET_CARD_SELECTOR).count()
        if cur >= per_query:
            break
        page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        new = _wait_for_new_cards(page, cur)
        if new <= prev and new <= cur:
            break
        prev = cur
    rows = []
    for c in scrape_search_results_from_dom(page):
        if not c.get("id"):
            continue
        if c.get("likes", 0) < min_likes:
            continue  # DOM 実測 likes で再検証
        c["query"] = q
        rows.append(c)
    return rows[:per_query]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", nargs="+", default=DEFAULT_QUERIES)
    ap.add_argument("--since", default=None, help="YYYY-MM-DD（省略時 = --days から計算）")
    ap.add_argument("--until", default=None, help="YYYY-MM-DD（省略時 = 今日）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-likes", type=int, default=DEFAULT_MIN_LIKES)
    ap.add_argument("--per-query", type=int, default=8)
    ap.add_argument("--profile", default="x_profiles/maaaki")
    ap.add_argument("--output-dir", default="/app/output/grok_twittora")
    args = ap.parse_args()

    now = datetime.now(JST)
    until = args.until or now.strftime("%Y-%m-%d")
    since = args.since or (now - timedelta(days=args.days)).strftime("%Y-%m-%d")

    cookies = fetch_bookmarks.load_cookies(args.profile)
    print(f"=== X Search Collect (@twittora_・login DOM 版) ===")
    print(f"queries: {len(args.queries)}, window: {since}..{until}, min_likes: {args.min_likes}")

    from playwright.sync_api import sync_playwright

    all_rows: list[dict] = []
    wall_errors = 0
    pw = browser = context = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=False, args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(**build_context_kwargs())
        context.add_cookies(cookies)
        page = context.new_page()
        for i, q in enumerate(args.queries):
            print(f"  → searching: {q!r}")
            try:
                rows = collect_query(page, q, since, until, args.min_likes, args.per_query)
            except LoginWallError as exc:
                print(f"  ✗ {exc}", file=sys.stderr)
                wall_errors += 1
                break  # 壁が出たら即停止（アカウント保護）
            except Exception as exc:  # goto タイムアウト等はクエリ単位 fail-soft（2026-07-19 修理）
                print(f"  ✗ {q!r} 失敗（スキップ）: {type(exc).__name__}: {str(exc)[:80]}", file=sys.stderr)
                time.sleep(random.uniform(5.0, 10.0))
                continue
            print(f"    got {len(rows)}")
            all_rows.extend(rows)
            if i < len(args.queries) - 1:
                time.sleep(random.uniform(*QUERY_PACING))
    finally:
        for closer in (context, browser):
            try:
                closer and closer.close()
            except Exception:
                pass
        try:
            pw and pw.stop()
        except Exception:
            pass

    # id 横断 dedupe
    seen, deduped = set(), []
    for r in all_rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        deduped.append(r)
    print(f"\nTotal: {len(all_rows)} fetched, {len(deduped)} unique")

    if not deduped:
        if wall_errors:
            print("ERROR: ログイン壁で停止。Cookie 更新（refresh-x-cookies）を確認", file=sys.stderr)
            return 1
        print("0件（窓内に閾値超えなし）。空ファイルは書かない")
        return 0

    captured = now.isoformat()
    for r in deduped:
        r.setdefault("display_name", "")
        r.setdefault("retweets", 0)
        r.setdefault("replies", 0)
        r["captured_at"] = captured
        r["collector"] = "x_search_dom"  # grok 版と区別（impressions=0 は未計測の意）

    today = now.date().isoformat()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / f"grok-twittora-{today}.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for r in deduped:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    md_path = out_dir / f"grok-twittora-{today}.md"
    lines = [
        "---",
        f"collected_at: {today}",
        f"entries: {len(deduped)}",
        f"window: {since}..{until}",
        'source: "x_search_collect_twittora.py (login DOM・impressions未計測)"',
        "---",
        "",
        f"# バズ収集 {today}（ログイン検索版・{len(deduped)}件）",
        "",
    ]
    for r in sorted(deduped, key=lambda x: x.get("likes", 0), reverse=True):
        lines.append(f"- **{r['likes']:,} likes** @{r['author']} ({r.get('posted_at','')}): {r['content'][:80]} … {r['url']}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✓ {jsonl_path}\n✓ {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
