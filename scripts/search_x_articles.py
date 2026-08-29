#!/usr/bin/env python3
"""search_x_articles.py — X の長文記事（X Articles）を「形式ごと」検索する。

2026-08-04 新設（ユーザー指示の核心: 「このような攻めの記事を、同じ投稿者からでなく広く集める」）。
鍵は実機検証済みの演算子 `url:x.com/i/article` — 長文記事を共有する投稿だけが返る
（filter:articles は0件で不発・お手本2記事の両著者＋未知の著者が同時にヒットすることを確認済み）。

トピック語 × url:x.com/i/article × min_faves(低floor) で「長文の活用術記事」を型ごと拾う。
コンテナ内（xstock-vnc）実行。出力: 1投稿1行の JSONL を stdout へ。
  docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/search_x_articles.py [--days 7] [--min-faves 5]
"""
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
from bookmarks_keyword_digest_collect_browser import build_context_kwargs
from x_search_collect_twittora import scrape_cards_rich  # カード抽出を再利用（正確指標つき）
from playwright.sync_api import sync_playwright

# 攻め記事の入口トピック（語彙は薄く・形式演算子が主役。floor 低め＝記事共有はいいねが付きにくい）
ARTICLE_QUERIES = [
    "Claude",
    "AI 自動化",
    "AI 削減",
    "AI エージェント",
    "AI 業務",
    "Obsidian 運用",  # 2026-08-09 追加: Obsidian×プロジェクト管理の長文（薄い語彙+形式演算子の既存設計思想どおり・「第二の脳」は最汚染語のため使わない）
]


def _offense_handles():
    """見張りリスト（lane=offense）のハンドル。読めなければ空リスト＝従来動作に退行するだけ。"""
    try:
        wl = json.loads(Path("/app/configs/x_watchlist.json").read_text(encoding="utf-8"))
        return [h["handle"] for h in wl.get("handles", []) if h.get("lane") == "offense"]
    except Exception:  # noqa: BLE001
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-faves", type=int, default=5)
    args = ap.parse_args()

    until = datetime.now(timezone.utc).date()
    since = until - timedelta(days=args.days)
    import os
    os.chdir("/app")
    cookies = fetch_bookmarks.load_cookies("x_profiles/maaaki")
    seen = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(**build_context_kwargs())
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        searches = [(f"{q} url:x.com/i/article min_faves:{args.min_faves} "
                     f"since:{since} until:{until} -filter:nativeretweets",
                     f"[article] {q}", "top") for q in ARTICLE_QUERIES]
        # P-MKA-33④ 2026-08-29: 定点著者（lane=offense）スコープの形式検索を追加。
        # グローバル検索は Top1画面ぶんの抽選＝定点著者の記事共有を取りこぼす（実測 2026-08-29:
        # 母集団を63件へ広げても対象記事が出ず `from:著者 url:x.com/i/article` だけがヒット）。
        # min_faves は付けない（記事共有はいいねが付きにくい＋著者は既に人選済み）。f=live で網羅。
        handles = _offense_handles()
        for i in range(0, len(handles), 6):
            scope = " OR ".join(f"from:{h}" for h in handles[i:i + 6])
            searches.append((f"({scope}) url:x.com/i/article since:{since} until:{until}",
                             f"[article-from] group{i // 6 + 1}", "live"))
        for full, label, tab in searches:
            url = f"https://x.com/search?q={quote(full)}&f={tab}"
            try:
                page.goto(url, timeout=30000)
                page.wait_for_timeout(6000)
                for row in scrape_cards_rich(page):
                    if row["url"] in seen:
                        continue
                    seen.add(row["url"])
                    row["query"] = label
                    row["format"] = "x_article_share"
                    print(json.dumps(row, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001
                print(f'{{"error": "{type(exc).__name__}", "query": "{label}"}}', file=sys.stderr)
            time.sleep(random.uniform(8, 15))  # 全クエリ×1ページのみ＝軽負荷（旧注記「5クエリ」は陳腐化のため更新）
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
