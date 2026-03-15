#!/usr/bin/env python3
"""投稿済みツイートのインプレッションを追跡するスクリプト

Usage:
    python scripts/track_impressions.py [--days 7] [--news-id ID] [--limit 10]

推奨スケジュール:
    - 投稿1h後、4h後、24h後
    - 以降、毎日1回を7日間
"""

import argparse
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extensions.tier3_posting.x_poster.post_store import PostStore

JST = timezone(timedelta(hours=9))


def _parse_count(text: str) -> int:
    """数値文字列をパース（1.2K, 2.3M など対応）。

    Args:
        text: 数値を含む文字列

    Returns:
        パースした整数値。パース失敗時は0
    """
    if not text:
        return 0

    text = text.strip().upper().replace(",", "")

    try:
        if "K" in text:
            return int(float(text.replace("K", "")) * 1000)
        elif "M" in text:
            return int(float(text.replace("M", "")) * 1000000)
        else:
            return int(text)
    except (ValueError, TypeError):
        return 0


def scrape_impressions(page, tweet_url: str) -> dict:
    """単一ツイートのインプレッション情報をスクレイプする。

    Args:
        page: Playwrightのページオブジェクト
        tweet_url: ツイートのURL

    Returns:
        インプレッションデータの辞書
    """
    result = {
        "tweet_url": tweet_url,
        "impressions": 0,
        "likes": 0,
        "retweets": 0,
        "replies": 0,
        "bookmarks": 0,
        "quotes": 0,
        "scraped_at": datetime.now(JST).isoformat(),
    }

    # ツイートページに遷移
    page.goto(tweet_url, wait_until="domcontentloaded")
    time.sleep(random.uniform(2.0, 4.0))

    # ツイートカードの読み込みを待機
    try:
        page.wait_for_selector('[data-testid="tweet"]', timeout=15000)
    except Exception:
        raise RuntimeError(f"ツイートの読み込みに失敗: {tweet_url}")

    time.sleep(random.uniform(1.0, 2.0))

    # --- メトリクス取得 ---

    # いいね数
    like_elem = page.query_selector('[data-testid="like"] span span')
    if like_elem:
        result["likes"] = _parse_count(like_elem.inner_text())

    # リツイート数
    retweet_elem = page.query_selector('[data-testid="retweet"] span span')
    if retweet_elem:
        result["retweets"] = _parse_count(retweet_elem.inner_text())

    # リプライ数
    reply_elem = page.query_selector('[data-testid="reply"] span span')
    if reply_elem:
        result["replies"] = _parse_count(reply_elem.inner_text())

    # ブックマーク数
    bookmark_elem = page.query_selector('[data-testid="bookmark"] span span')
    if bookmark_elem:
        result["bookmarks"] = _parse_count(bookmark_elem.inner_text())

    # インプレッション数（ツイート詳細ページの閲覧数表示）
    # analyticsButton または viewCount 等のセレクタで取得を試みる
    for selector in [
        '[data-testid="analyticsButton"] span span',
        '[aria-label*="view"] span',
        '[aria-label*="表示"] span',
        'a[href*="/analytics"] span span',
    ]:
        try:
            elem = page.query_selector(selector)
            if elem:
                text = elem.inner_text().strip()
                count = _parse_count(text)
                if count > 0:
                    result["impressions"] = count
                    break
        except Exception:
            continue

    # エンゲージメント率の計算
    total_engagement = (
        result["likes"]
        + result["retweets"]
        + result["replies"]
        + result["bookmarks"]
    )
    if result["impressions"] > 0:
        result["engagement_rate"] = round(
            total_engagement / result["impressions"], 6
        )
    else:
        result["engagement_rate"] = 0.0

    return result


def main():
    parser = argparse.ArgumentParser(
        description="投稿済みツイートのインプレッションを追跡"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="過去N日以内の投稿を対象（デフォルト: 7）",
    )
    parser.add_argument(
        "--news-id",
        help="特定のnews_idのみ追跡",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="1回の実行で追跡するツイート数上限（デフォルト: 10）",
    )
    parser.add_argument(
        "--profile",
        default="./x_profile",
        help="ブラウザプロファイルパス（デフォルト: ./x_profile）",
    )
    parser.add_argument(
        "--output",
        default="./output/posting",
        help="出力ディレクトリ（デフォルト: ./output/posting）",
    )
    parser.add_argument(
        "--wait",
        nargs=2,
        type=float,
        default=[60, 120],
        metavar=("MIN", "MAX"),
        help="スクレイプ間の待機秒数（デフォルト: 60 120）",
    )
    args = parser.parse_args()

    store = PostStore(base_dir=args.output)

    # 投稿履歴から対象ツイートを取得
    history = store.load_history()
    posted = [
        rec
        for rec in history
        if rec.get("status") == "posted"
        and rec.get("posted_url")
        and not rec.get("dry_run")
    ]

    if not posted:
        print("投稿済みツイートがありません")
        return

    # フィルタリング
    if args.news_id:
        posted = [rec for rec in posted if rec.get("news_id") == args.news_id]
        if not posted:
            print(f"news_id '{args.news_id}' の投稿が見つかりません")
            return
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
        cutoff_iso = cutoff.isoformat()
        posted = [
            rec
            for rec in posted
            if rec.get("posted_at", "") >= cutoff_iso
        ]
        if not posted:
            print(f"過去{args.days}日以内の投稿がありません")
            return

    # posted_urlが無いものを除外（安全策）
    posted = [rec for rec in posted if rec.get("posted_url")]

    # 重複排除（同じnews_idが複数ある場合は最新のみ）
    seen_ids = {}
    for rec in posted:
        nid = rec.get("news_id", "")
        if nid not in seen_ids:
            seen_ids[nid] = rec
        else:
            existing_at = seen_ids[nid].get("posted_at", "")
            current_at = rec.get("posted_at", "")
            if current_at > existing_at:
                seen_ids[nid] = rec
    posted = list(seen_ids.values())

    # limit適用
    targets = posted[: args.limit]

    print(f"{'='*60}")
    print(f"インプレッション追跡")
    print(f"  対象: {len(targets)}件 (全{len(posted)}件中)")
    print(f"  期間: 過去{args.days}日")
    print(f"  待機: {args.wait[0]:.0f}-{args.wait[1]:.0f}秒")
    print(f"{'='*60}\n")

    # Playwright起動
    from pathlib import Path

    from playwright.sync_api import sync_playwright

    profile_path = Path(args.profile).resolve()
    cookie_file = profile_path / "cookies.json"

    try:
        from collector.cookie_crypto import load_cookies_encrypted

        cookies = load_cookies_encrypted(cookie_file)
    except Exception as exc:
        print(f"Cookie読込エラー: {exc}")
        sys.exit(1)

    if not cookies:
        print("Cookie読込失敗: cookies.jsonが見つからないか空です")
        sys.exit(1)

    print(f"Cookie読込完了 ({len(cookies)}個)\n")

    tracked_count = 0
    failed_count = 0
    all_impressions = []

    pw = None
    browser = None
    context = None

    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )
        context.add_cookies(cookies)
        page = context.new_page()

        # ログイン確認
        page.goto("https://twitter.com/home", wait_until="domcontentloaded")
        time.sleep(random.uniform(2.0, 4.0))

        try:
            page.wait_for_selector(
                '[data-testid="SideNav_AccountSwitcher_Button"], '
                '[data-testid="AppTabBar_Home_Link"]',
                timeout=10000,
            )
        except Exception:
            print("ログインしていません。setup_profile.pyを実行してください")
            sys.exit(1)

        print("ログイン確認OK\n")

        for i, rec in enumerate(targets):
            news_id = rec.get("news_id", "unknown")
            posted_url = rec["posted_url"]
            posted_at = rec.get("posted_at", "不明")

            print(
                f"[{i+1}/{len(targets)}] {news_id}"
            )
            print(f"  URL: {posted_url}")
            print(f"  投稿日時: {posted_at}")

            try:
                result = scrape_impressions(page, posted_url)
                result["news_id"] = news_id

                store.add_impression(result)

                print(
                    f"  \U0001f4ca "
                    f"imp={result['impressions']:,} "
                    f"like={result['likes']:,} "
                    f"rt={result['retweets']:,} "
                    f"reply={result['replies']:,} "
                    f"bm={result['bookmarks']:,} "
                    f"eng={result['engagement_rate']:.4%}"
                )

                tracked_count += 1
                all_impressions.append(result)

            except Exception as exc:
                print(f"  \u274c 取得失敗: {exc}")
                failed_count += 1

            # 最後のツイート以外は待機
            if i < len(targets) - 1:
                wait_sec = random.uniform(args.wait[0], args.wait[1])
                print(f"  \u23f3 次のスクレイプまで {wait_sec:.0f}秒 待機...")
                time.sleep(wait_sec)

            print()

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

    # サマリー表示
    print(f"{'='*60}")
    print(f"追跡結果サマリー:")
    print(f"  成功: {tracked_count}件")
    print(f"  失敗: {failed_count}件")

    if all_impressions:
        avg_imp = sum(r["impressions"] for r in all_impressions) / len(
            all_impressions
        )
        avg_likes = sum(r["likes"] for r in all_impressions) / len(
            all_impressions
        )
        avg_eng = sum(r["engagement_rate"] for r in all_impressions) / len(
            all_impressions
        )
        print(f"\n  平均インプレッション: {avg_imp:,.0f}")
        print(f"  平均いいね: {avg_likes:,.0f}")
        print(f"  平均エンゲージメント率: {avg_eng:.4%}")

        # トップパフォーマー
        top = max(all_impressions, key=lambda r: r["impressions"])
        print(f"\n  \U0001f3c6 トップパフォーマー:")
        print(f"    news_id: {top['news_id']}")
        print(f"    インプレッション: {top['impressions']:,}")
        print(f"    いいね: {top['likes']:,}")
        print(f"    エンゲージメント率: {top['engagement_rate']:.4%}")
        print(f"    URL: {top['tweet_url']}")

    print(f"\n  データ保存先: {store.impressions_path}")


if __name__ == "__main__":
    main()
