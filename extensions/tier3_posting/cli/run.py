#!/usr/bin/env python3
"""投稿実行CLI。予約投稿と即時投稿を統一的に処理する。

schedule.py / post.py の共通ロジックを統合。
account_routing によるマルチアカウント対応、post_preparation による本文組立を使用。

Usage:
    # 予約投稿（デフォルト）
    python -m extensions.tier3_posting.cli.run --no-dry-run --limit 5

    # 即時投稿
    python -m extensions.tier3_posting.cli.run --mode immediate --no-dry-run

    # JSONインポート
    python -m extensions.tier3_posting.cli.run --import-json export.json
"""

import argparse
import os
import random
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from dateutil.parser import parse as parse_datetime

from ..account_routing import get_account_label, get_profile_path, resolve_account
from ..services.post_preparation import DEFAULT_OFFSET_DAYS, build_final_post_text
from ..x_poster.post_store import PostStore
from ..x_poster.poster import XPoster

JST = timezone(timedelta(hours=9))

# 即時投稿時の待機時間（秒）
IMMEDIATE_WAIT_MIN = 60
IMMEDIATE_WAIT_MAX = 120

# 予約投稿時の待機時間（秒）
SCHEDULE_WAIT_MIN = 3
SCHEDULE_WAIT_MAX = 5


def parse_args(argv: List[str] = None) -> argparse.Namespace:
    """CLI引数をパースする。

    Args:
        argv: 引数リスト（None時はsys.argv）

    Returns:
        パース済み Namespace
    """
    parser = argparse.ArgumentParser(
        description="承認済みドラフトをXに投稿（予約/即時）",
    )
    parser.add_argument(
        "--mode",
        choices=["schedule", "immediate"],
        default="schedule",
        help="投稿モード: schedule（予約、デフォルト）/ immediate（即時）",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="本番モード（デフォルトはdry_run）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="最大投稿数（デフォルト: 5）",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="投稿間隔（分、scheduleモード）（デフォルト: 60）",
    )
    parser.add_argument(
        "--scheduled-at",
        help="最初の予約日時（ISO 8601 JST, 例: 2026-03-15T09:00:00+09:00）",
    )
    parser.add_argument(
        "--offset-days",
        type=int,
        default=DEFAULT_OFFSET_DAYS,
        help=f"予約日時未指定時のオフセット日数（デフォルト: {DEFAULT_OFFSET_DAYS}）",
    )
    parser.add_argument(
        "--import-json",
        help="review.htmlからエクスポートしたJSONを取込",
    )
    parser.add_argument(
        "--profile",
        default="./x_profile",
        help="ブラウザプロファイルパス（account_id未設定時のフォールバック）",
    )
    parser.add_argument(
        "--output",
        default="./output/posting",
        help="PostStoreディレクトリ",
    )
    return parser.parse_args(argv)


def calculate_scheduled_time(
    args: argparse.Namespace,
    interval_index: int,
) -> datetime:
    """interval_index 番目の予約日時を算出する。

    Args:
        args: CLI引数（scheduled_at, offset_days, interval を参照）
        interval_index: 予約枠のインデックス（0始まり）

    Returns:
        timezone-aware な datetime (JST)
    """
    if args.scheduled_at:
        base = parse_datetime(args.scheduled_at)
        if base.tzinfo is None:
            base = base.replace(tzinfo=JST)
    else:
        base = datetime.now(JST) + timedelta(days=args.offset_days)

    return base + timedelta(minutes=args.interval * interval_index)


def extract_image_paths(draft: dict) -> List[str]:
    """ドラフトから存在する画像パスを抽出する。

    Args:
        draft: ドラフト辞書

    Returns:
        存在する画像パスのリスト
    """
    return [
        img["path"]
        for img in draft.get("images", [])
        if os.path.exists(img.get("path", ""))
    ]


def execute_post(
    poster: XPoster,
    post_body: str,
    image_paths: List[str],
    mode: str,
    scheduled_time: datetime = None,
    dry_run: bool = True,
) -> dict:
    """投稿を実行する（schedule / immediate を分岐）。

    Args:
        poster: XPoster インスタンス
        post_body: 最終投稿テキスト
        image_paths: 画像パスリスト
        mode: "schedule" or "immediate"
        scheduled_time: 予約日時（scheduleモード時のみ必須）
        dry_run: ドライランフラグ

    Returns:
        XPoster の返却辞書
    """
    images = image_paths or None

    if mode == "schedule":
        return poster.schedule_post(
            body=post_body,
            scheduled_at=scheduled_time.isoformat(),
            images=images,
            dry_run=dry_run,
        )
    else:
        return poster.post(
            body=post_body,
            images=images,
            dry_run=dry_run,
        )


def build_success_history(
    news_id: str,
    mode: str,
    now: str,
    dry_run: bool,
    account_id: str,
    result: dict,
    scheduled_time: datetime = None,
) -> dict:
    """成功時の履歴エントリを組み立てる。

    Args:
        news_id: ニュースID
        mode: "schedule" or "immediate"
        now: 現在日時 ISO文字列
        dry_run: ドライランフラグ
        account_id: アカウントID
        result: XPoster の返却辞書
        scheduled_time: 予約日時（scheduleモード）

    Returns:
        履歴辞書
    """
    if mode == "schedule":
        return {
            "news_id": news_id,
            "status": "scheduled",
            "scheduled_at": scheduled_time.isoformat() if scheduled_time else "",
            "recorded_at": now,
            "dry_run": dry_run,
            "account_id": account_id,
        }
    else:
        return {
            "news_id": news_id,
            "status": "posted",
            "posted_url": result.get("posted_url", ""),
            "posted_at": now,
            "dry_run": result.get("dry_run", False),
            "account_id": account_id,
        }


def build_failure_history(
    news_id: str,
    error: str,
    now: str,
    account_id: str,
) -> dict:
    """失敗時の履歴エントリを組み立てる。

    Args:
        news_id: ニュースID
        error: エラーメッセージ
        now: 現在日時 ISO文字列
        account_id: アカウントID

    Returns:
        履歴辞書
    """
    return {
        "news_id": news_id,
        "status": "failed",
        "error": error,
        "attempted_at": now,
        "account_id": account_id,
    }


def main(argv: List[str] = None) -> None:
    """メインエントリポイント。

    Args:
        argv: CLI引数リスト（テスト用。None時はsys.argv）
    """
    args = parse_args(argv)
    dry_run = not args.no_dry_run
    mode = args.mode

    store = PostStore(base_dir=args.output)

    # 1. Import if specified
    if args.import_json:
        if not os.path.exists(args.import_json):
            print(f"ERROR: ファイルが見つかりません: {args.import_json}")
            sys.exit(1)
        count = store.import_from_json(args.import_json)
        print(f"JSON取込完了: {count}件")

    # 2. Load approved drafts
    approved = store.load_drafts(status_filter="approved")
    if not approved:
        print("承認済みドラフトがありません")
        return

    # Header
    mode_label = "予約投稿" if mode == "schedule" else "即時投稿"
    print(f"承認済みドラフト: {len(approved)}件")
    print(f"投稿モード: {mode_label} ({'DRY RUN' if dry_run else '本番投稿'})")
    print(f"最大投稿数: {args.limit}")
    if mode == "schedule":
        base_time = calculate_scheduled_time(args, 0)
        print(f"投稿間隔: {args.interval}分")
        print(f"最初の予約: {base_time.isoformat()}")
    print(f"{'=' * 60}\n")

    # 3. Process each draft
    posters: Dict[str, XPoster] = {}  # account_id -> XPoster cache
    results = {"success": 0, "failed": 0, "skipped": 0}
    interval_index = 0  # skips don't increment

    effective_limit = min(len(approved), args.limit)

    for i, draft in enumerate(approved[:args.limit]):
        news_id = draft.get("news_id", "unknown")
        title = draft.get("title", "無題")
        body = draft.get("body", "")

        print(f"[{i + 1}/{effective_limit}] {title}")

        # Skip if already posted or scheduled
        if store.is_posted(news_id):
            print("  スキップ（投稿済み）")
            results["skipped"] += 1
            continue

        if not body:
            print("  スキップ（本文が空）")
            results["skipped"] += 1
            continue

        # Resolve account
        account_id = draft.get("account_id") or resolve_account(draft)
        profile = get_profile_path(account_id)
        account_label = get_account_label(account_id)

        # Get or create poster
        if account_id not in posters:
            posters[account_id] = XPoster(profile_path=profile)
        poster = posters[account_id]

        print(f"  アカウント: @{account_label} (profile: {profile})")

        # Build final text
        post_body = build_final_post_text(body, draft.get("hashtags"))

        # Extract images
        image_paths = extract_image_paths(draft)

        # Determine scheduled time (schedule mode)
        scheduled_time = None
        if mode == "schedule":
            draft_scheduled_at = draft.get("scheduled_at")
            if draft_scheduled_at:
                scheduled_time = parse_datetime(draft_scheduled_at)
                if scheduled_time.tzinfo is None:
                    scheduled_time = scheduled_time.replace(tzinfo=JST)
            else:
                scheduled_time = calculate_scheduled_time(args, interval_index)
            print(f"  予約日時: {scheduled_time.isoformat()}")

        # Execute
        now = datetime.now(timezone.utc).isoformat()
        try:
            result = execute_post(
                poster=poster,
                post_body=post_body,
                image_paths=image_paths,
                mode=mode,
                scheduled_time=scheduled_time,
                dry_run=dry_run,
            )
        except Exception as exc:
            print(f"  エラー: {exc}")
            traceback.print_exc()
            store.add_history(build_failure_history(news_id, str(exc), now, account_id))
            if not dry_run:
                store.update_draft_status(news_id, "failed")
            results["failed"] += 1
            interval_index += 1
            continue

        # Handle result
        if result.get("success"):
            if mode == "schedule":
                print(f"  予約成功: {scheduled_time.isoformat()}")
                target_status = "scheduled"
            else:
                print(f"  投稿成功: {result.get('posted_url', '(dry run)')}")
                target_status = "posted"

            store.add_history(
                build_success_history(
                    news_id=news_id,
                    mode=mode,
                    now=now,
                    dry_run=dry_run,
                    account_id=account_id,
                    result=result,
                    scheduled_time=scheduled_time,
                )
            )
            if not dry_run:
                store.update_draft_status(news_id, target_status, account_id=account_id)
            results["success"] += 1
        else:
            error_msg = result.get("error", "Unknown error")
            print(f"  失敗: {error_msg}")
            store.add_history(build_failure_history(news_id, error_msg, now, account_id))
            if not dry_run:
                store.update_draft_status(news_id, "failed")
            results["failed"] += 1

        interval_index += 1

        # Wait between posts
        is_last = i >= effective_limit - 1
        if not is_last and not dry_run:
            if mode == "schedule":
                wait_sec = random.uniform(SCHEDULE_WAIT_MIN, SCHEDULE_WAIT_MAX)
            else:
                wait_sec = random.uniform(IMMEDIATE_WAIT_MIN, IMMEDIATE_WAIT_MAX)
            print(f"  次の投稿まで {wait_sec:.0f}秒 待機...")
            time.sleep(wait_sec)

    # Summary
    print(f"\n{'=' * 60}")
    print(f"{mode_label}結果サマリー:")
    print(f"  成功: {results['success']}件")
    print(f"  失敗: {results['failed']}件")
    print(f"  スキップ: {results['skipped']}件")
    if results["success"] > 0:
        accounts_used = ", ".join(
            f"@{get_account_label(a)}" for a in posters.keys()
        )
        print(f"  使用アカウント: {accounts_used}")
    if dry_run:
        print(
            f"\n注意: ドライランモードです。実投稿するには --no-dry-run を指定してください。"
        )


if __name__ == "__main__":
    main()
