#!/usr/bin/env python3
"""承認済みドラフトをXに予約投稿するスクリプト

Usage:
    python scripts/schedule_approved.py [--no-dry-run] [--limit 5] [--interval 60]
    python scripts/schedule_approved.py --scheduled-at "2026-03-15T09:00:00+09:00"
    python scripts/schedule_approved.py --offset-days 30
"""

import argparse
import os
import random
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extensions.tier3_posting.x_poster.post_store import PostStore
from extensions.tier3_posting.x_poster.poster import XPoster

JST = timezone(timedelta(hours=9))


def main():
    parser = argparse.ArgumentParser(description="承認済みドラフトをXに予約投稿")
    parser.add_argument("--import-json", help="review.htmlからエクスポートしたJSONを取込")
    parser.add_argument("--no-dry-run", action="store_true", help="本番モード（デフォルトはdry_run）")
    parser.add_argument("--limit", type=int, default=5, help="最大投稿数（デフォルト: 5）")
    parser.add_argument("--interval", type=int, default=60, help="投稿間隔（分）（デフォルト: 60）")
    parser.add_argument(
        "--scheduled-at",
        help="最初の予約日時（ISO 8601 JST, 例: 2026-03-15T09:00:00+09:00）。未指定時は現在+offset-days日後",
    )
    parser.add_argument("--offset-days", type=int, default=30, help="未指定時のオフセット日数（デフォルト: 30）")
    parser.add_argument("--profile", default="./x_profile", help="ブラウザプロファイルパス")
    parser.add_argument("--output", default="./output/posting", help="出力ディレクトリ")
    args = parser.parse_args()

    dry_run = not args.no_dry_run

    store = PostStore(base_dir=args.output)

    # Import from JSON if specified
    if args.import_json:
        if not os.path.exists(args.import_json):
            print(f"ERROR: ファイルが見つかりません: {args.import_json}")
            sys.exit(1)
        count = store.import_from_json(args.import_json)
        print(f"JSON取込完了: {count}件")

    # Load approved drafts
    approved = store.load_drafts(status_filter="approved")
    if not approved:
        print("承認済みドラフトがありません")
        return

    # Calculate first scheduled time
    if args.scheduled_at:
        from dateutil.parser import parse as parse_datetime
        first_scheduled = parse_datetime(args.scheduled_at)
        if first_scheduled.tzinfo is None:
            first_scheduled = first_scheduled.replace(tzinfo=JST)
    else:
        first_scheduled = datetime.now(JST) + timedelta(days=args.offset_days)

    print(f"承認済みドラフト: {len(approved)}件")
    print(f"投稿モード: {'DRY RUN' if dry_run else '本番投稿'}")
    print(f"最大投稿数: {args.limit}")
    print(f"投稿間隔: {args.interval}分")
    print(f"最初の予約: {first_scheduled.isoformat()}")
    print(f"{'='*60}\n")

    poster = XPoster(profile_path=args.profile)

    scheduled_count = 0
    failed_count = 0
    skipped_count = 0

    for i, draft in enumerate(approved[:args.limit]):
        news_id = draft.get("news_id", "unknown")
        title = draft.get("title", "無題")
        body = draft.get("body", "")

        # Calculate scheduled time for this draft
        scheduled_time = first_scheduled + timedelta(minutes=args.interval * i)

        print(f"[{i+1}/{min(len(approved), args.limit)}] {title}")
        print(f"  予約日時: {scheduled_time.isoformat()}")

        # Skip if already posted or scheduled
        if store.is_posted(news_id):
            print(f"  スキップ（投稿済み）")
            skipped_count += 1
            continue

        if not body:
            print(f"  スキップ（本文が空）")
            skipped_count += 1
            continue

        # Add hashtags to body
        hashtags = draft.get("hashtags", [])
        post_body = body
        if hashtags:
            tags_str = " ".join(hashtags)
            if len(post_body) + len(tags_str) + 2 <= 280:
                post_body = f"{post_body}\n\n{tags_str}"

        # Schedule post
        try:
            result = poster.schedule_post(
                body=post_body,
                scheduled_at=scheduled_time.isoformat(),
                dry_run=dry_run,
            )
        except Exception as exc:
            print(f"  エラー: {exc}")
            traceback.print_exc()
            failed_count += 1
            continue

        now = datetime.now(timezone.utc).isoformat()

        if result.get("success"):
            print(f"  予約成功: {scheduled_time.isoformat()}")
            store.add_history({
                "news_id": news_id,
                "status": "scheduled",
                "scheduled_at": scheduled_time.isoformat(),
                "recorded_at": now,
                "dry_run": dry_run,
            })
            if not dry_run:
                store.update_draft_status(news_id, "scheduled")
            scheduled_count += 1
        else:
            print(f"  失敗: {result.get('error', 'Unknown error')}")
            store.add_history({
                "news_id": news_id,
                "status": "failed",
                "error": result.get("error", ""),
                "attempted_at": now,
            })
            store.update_draft_status(news_id, "failed")
            failed_count += 1

        # Wait between operations
        if i < min(len(approved), args.limit) - 1:
            wait_sec = random.uniform(3, 5)
            print(f"  次の予約まで {wait_sec:.0f}秒 待機...")
            time.sleep(wait_sec)

    # Summary
    print(f"\n{'='*60}")
    print(f"予約投稿結果サマリー:")
    print(f"  予約成功: {scheduled_count}件")
    print(f"  失敗: {failed_count}件")
    print(f"  スキップ: {skipped_count}件")
    if scheduled_count > 0:
        last_scheduled = first_scheduled + timedelta(minutes=args.interval * (scheduled_count - 1))
        print(f"  予約範囲: {first_scheduled.strftime('%H:%M')} ~ {last_scheduled.strftime('%H:%M')}")
    if dry_run:
        print(f"\n注意: ドライランモードです。実投稿するには --no-dry-run を指定してください。")


if __name__ == "__main__":
    main()
