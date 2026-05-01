#!/usr/bin/env python3
"""投稿済みツイートのインプレッションを追跡するスクリプト。

plan.md M1 T1.1 で `ImpressionScraper.scrape_batch()` (Canonical) に統合。
本ファイルは PostStore からの対象抽出 + 結果書き込みの薄い CLI ラッパー。

Usage:
    python -m extensions.tier3_posting.cli.track [--days 7] [--news-id ID] [--limit 10]

推奨スケジュール:
    - 投稿1h後、4h後、24h後
    - 以降、毎日1回を7日間
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..impression_tracker.scraper import ImpressionScraper
from ..shared.exceptions import CookieExpiredError
from ..x_poster.post_store import PostStore

JST = timezone(timedelta(hours=9))


def _build_targets(
    store: PostStore, days: int, news_id: str, limit: int
) -> List[Dict[str, Any]]:
    """PostStore 履歴から追跡対象ツイートを抽出する。"""
    history = store.load_history()
    posted = [
        rec for rec in history
        if rec.get("status") == "posted"
        and rec.get("posted_url")
        and not rec.get("dry_run")
    ]
    if news_id:
        posted = [rec for rec in posted if rec.get("news_id") == news_id]
    else:
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        posted = [rec for rec in posted if rec.get("posted_at", "") >= cutoff_iso]

    # news_id 重複は最新のみ
    seen: Dict[str, Dict[str, Any]] = {}
    for rec in posted:
        nid = rec.get("news_id", "")
        if nid not in seen or rec.get("posted_at", "") > seen[nid].get("posted_at", ""):
            seen[nid] = rec
    return list(seen.values())[:limit]


def _build_schedule_targets(store: PostStore, limit: int) -> List[Dict[str, Any]]:
    """plan.md M1 T1.2 follow-up: 期限到来した予約追跡エントリを取得（url/news_id/interval_hours 付き）。"""
    due = store.load_due_schedules()
    # 追跡対象フォーマットに合わせる（posted_url が主キー）
    targets = []
    for rec in due:
        targets.append({
            "news_id": rec.get("news_id"),
            "posted_url": rec.get("tweet_url", ""),
            "posted_at": rec.get("scheduled_at", ""),
            "account_id": rec.get("account_id", ""),
            "interval_hours": rec.get("interval_hours"),
            "_source": "schedule",
        })
    return targets[:limit]


def _to_impression_record(
    scrape_result: Dict[str, Any],
    news_id: str,
    account_id: str = "",
    interval_hours: Optional[float] = None,
) -> Dict[str, Any]:
    """ImpressionScraper の T0 スキーマを PostStore.add_impression のスキーマに変換。"""
    likes = scrape_result.get("likes", 0) or 0
    retweets = scrape_result.get("retweets", 0) or 0
    replies = scrape_result.get("replies", 0) or 0
    bookmarks = scrape_result.get("bookmarks", 0) or 0
    views = scrape_result.get("views", 0) or 0
    total_eng = likes + retweets + replies + bookmarks
    eng_rate = round(total_eng / views, 6) if views > 0 else 0.0
    rec = {
        "news_id": news_id,
        "tweet_url": scrape_result.get("url", ""),
        "impressions": views,
        "likes": likes,
        "retweets": retweets,
        "replies": replies,
        "bookmarks": bookmarks,
        "engagement_rate": eng_rate,
        "scraped_at": scrape_result.get("scraped_at", ""),
        "status": scrape_result.get("status", "unknown"),
    }
    if account_id:
        rec["account_id"] = account_id
    if interval_hours is not None:
        rec["interval_hours"] = interval_hours
    return rec


def main() -> int:
    parser = argparse.ArgumentParser(description="投稿済みツイートのインプレッションを追跡")
    parser.add_argument("--days", type=int, default=7, help="過去N日以内（デフォルト: 7）")
    parser.add_argument("--news-id", help="特定の news_id のみ")
    parser.add_argument("--limit", type=int, default=10, help="1回の上限（デフォルト: 10）")
    parser.add_argument("--profile", default="./x_profile", help="プロファイルパス")
    parser.add_argument("--output", default="./output/posting", help="出力ディレクトリ")
    parser.add_argument("--screenshot-dir", default=None, help="エラー時 SS 保存先")
    parser.add_argument(
        "--from-schedule",
        action="store_true",
        help="plan.md M1 T1.2: add_impression_schedule で記録した予約エントリのうち期限到来分を取得",
    )
    args = parser.parse_args()

    store = PostStore(base_dir=args.output)
    if args.from_schedule:
        targets = _build_schedule_targets(store, args.limit)
        mode_label = "予約エントリ (scheduled_at 到来分)"
    else:
        targets = _build_targets(store, args.days, args.news_id, args.limit)
        mode_label = f"過去{args.days}日"

    if not targets:
        print("追跡対象がありません")
        return 0

    print(f"{'='*60}")
    print(f"インプレッション追跡 (Canonical: ImpressionScraper.scrape_batch)")
    print(f"  対象: {len(targets)}件 / {mode_label}")
    print(f"{'='*60}\n")

    url_to_meta = {
        rec["posted_url"]: {
            "news_id": rec.get("news_id", "unknown"),
            "account_id": rec.get("account_id", ""),
            "interval_hours": rec.get("interval_hours"),
        }
        for rec in targets
    }
    urls = list(url_to_meta.keys())

    scraper = ImpressionScraper(profile_path=args.profile, screenshot_dir=args.screenshot_dir)
    try:
        results = scraper.scrape_batch(urls)
    except CookieExpiredError as e:
        print(f"[ERROR] Cookie 期限切れ: {e}", file=sys.stderr)
        return 2

    tracked, failed = 0, 0
    all_impressions: List[Dict[str, Any]] = []
    for r in results:
        meta = url_to_meta.get(r.get("url", ""), {"news_id": "unknown"})
        nid = meta["news_id"]
        rec = _to_impression_record(
            r, nid,
            account_id=meta.get("account_id", ""),
            interval_hours=meta.get("interval_hours"),
        )
        if rec["status"] == "ok":
            store.add_impression(rec)
            tracked += 1
            all_impressions.append(rec)
            print(
                f"  ✓ {nid} imp={rec['impressions']:,} "
                f"like={rec['likes']:,} rt={rec['retweets']:,} "
                f"eng={rec['engagement_rate']:.4%}"
            )
        else:
            failed += 1
            print(f"  ✗ {nid} status={rec['status']} error={r.get('error_detail', '')}")

    print(f"\n{'='*60}")
    print(f"結果: 成功 {tracked} / 失敗 {failed}")
    if all_impressions:
        avg_imp = sum(r["impressions"] for r in all_impressions) / len(all_impressions)
        avg_eng = sum(r["engagement_rate"] for r in all_impressions) / len(all_impressions)
        print(f"  平均 impressions: {avg_imp:,.0f}")
        print(f"  平均 engagement_rate: {avg_eng:.4%}")
    print(f"  保存先: {store.impressions_path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
