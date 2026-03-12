#!/usr/bin/env python3
"""承認済みドラフトをXに投稿するスクリプト。"""

import argparse
import os
import random
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extensions.tier3_posting.x_poster.post_store import PostStore
from extensions.tier3_posting.x_poster.poster import XPoster


def main():
    parser = argparse.ArgumentParser(description="承認済みドラフトをXに投稿")
    parser.add_argument("--import-json", help="review.htmlからエクスポートしたJSONを取込")
    parser.add_argument("--dry-run", action="store_true", default=True, help="ドライラン（デフォルト有効）")
    parser.add_argument("--no-dry-run", action="store_true", help="実投稿モード")
    parser.add_argument("--limit", type=int, default=5, help="最大投稿数（デフォルト: 5）")
    parser.add_argument("--profile", default="./x_profile", help="ブラウザプロファイルパス")
    parser.add_argument("--data-dir", default="output/posting", help="PostStoreのベースディレクトリ")
    args = parser.parse_args()

    dry_run = not args.no_dry_run

    store = PostStore(base_dir=args.data_dir)

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

    print(f"承認済みドラフト: {len(approved)}件")
    print(f"投稿モード: {'DRY RUN' if dry_run else '本番投稿'}")
    print(f"最大投稿数: {args.limit}")
    print(f"{'='*60}\n")

    poster = XPoster(profile_path=args.profile)

    posted_count = 0
    failed_count = 0
    skipped_count = 0

    for i, draft in enumerate(approved[:args.limit]):
        news_id = draft.get("news_id", "unknown")
        title = draft.get("title", "無題")
        body = draft.get("body", "")

        print(f"[{i+1}/{min(len(approved), args.limit)}] {title}")

        # Skip if already posted
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

        # Post
        result = poster.post(body=post_body, dry_run=dry_run)

        now = datetime.now(timezone.utc).isoformat()

        if result.get("success"):
            print(f"  成功: {result.get('posted_url', '(dry run)')}")
            store.add_history({
                "news_id": news_id,
                "status": "posted",
                "posted_url": result.get("posted_url", ""),
                "posted_at": now,
                "dry_run": result.get("dry_run", False),
            })
            if not result.get("dry_run"):
                store.update_draft_status(news_id, "posted")
            posted_count += 1
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

        # Wait between posts (skip for dry run)
        if not dry_run and i < min(len(approved), args.limit) - 1:
            wait_sec = random.uniform(60, 120)
            print(f"  次の投稿まで {wait_sec:.0f}秒 待機...")
            time.sleep(wait_sec)

    # Summary
    print(f"\n{'='*60}")
    print(f"投稿結果サマリー:")
    print(f"  成功: {posted_count}件")
    print(f"  失敗: {failed_count}件")
    print(f"  スキップ: {skipped_count}件")
    if dry_run:
        print(f"\n注意: ドライランモードです。実投稿するには --no-dry-run を指定してください。")


if __name__ == "__main__":
    main()
