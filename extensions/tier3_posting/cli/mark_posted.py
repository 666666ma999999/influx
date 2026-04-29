#!/usr/bin/env python3
"""外部プロジェクトから投稿後ステータスを更新する CLI。

外部プロジェクトが PostStore を直接 import する代わりに、本 CLI を subprocess 経由で
呼び出す。投稿履歴 (history) と draft status の両方を更新する。

Usage:
    python -m extensions.tier3_posting.cli.mark_posted \\
        --news-id <hex16> \\
        --posted-url "https://x.com/..." \\
        [--dry-run]

    # ステータスのみ更新（history は触らない）
    python -m extensions.tier3_posting.cli.mark_posted \\
        --news-id <hex16> \\
        --status posted \\
        --no-history

Output (stdout, 1行 JSON):
    {"news_id": "...", "ok": true, "actions": ["history", "status"]}

Exit codes:
    0: 成功
    1: 実行時エラー
    2: 引数不足
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..x_poster.post_store import PostStore


def _resolve_post_store(base_dir: Optional[str] = None) -> PostStore:
    if base_dir:
        return PostStore(base_dir=base_dir)
    repo_root = Path(__file__).resolve().parents[3]
    return PostStore(base_dir=str(repo_root / "output" / "posting"))


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="外部プロジェクトの投稿後ステータスを influx PostStore に反映",
    )
    parser.add_argument("--news-id", required=True)
    parser.add_argument("--status", default="posted", help="新ステータス（既定: posted）")
    parser.add_argument("--posted-url", default="", help="投稿後 URL（履歴に記録）")
    parser.add_argument("--dry-run", action="store_true", help="status は更新せず history のみ記録")
    parser.add_argument("--no-history", action="store_true", help="history を記録しない")
    parser.add_argument("--base-dir", help="PostStore ベースディレクトリ（テスト用）")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        store = _resolve_post_store(args.base_dir)
    except Exception as e:
        print(f"ERROR: PostStore 初期化失敗: {e}", file=sys.stderr)
        return 1

    actions = []
    now = datetime.now(timezone.utc).isoformat()

    # 順序: status 更新 → history。history 先行だと is_posted() が
    # status 未更新でも True を返す半適用状態を作る (post_store.py is_posted 依存)。
    if not args.dry_run:
        try:
            ok = store.update_draft_status(args.news_id, args.status)
        except Exception as e:
            print(f"ERROR: status 更新失敗: {e}", file=sys.stderr)
            return 1
        if not ok:
            print(json.dumps(
                {"news_id": args.news_id, "ok": False, "actions": actions, "error": "news_id not found"},
                ensure_ascii=False,
            ))
            return 1
        actions.append("status")

    if not args.no_history:
        try:
            store.add_history({
                "news_id": args.news_id,
                "status": args.status,
                "posted_at": now,
                "posted_url": args.posted_url,
                "dry_run": args.dry_run,
            })
            actions.append("history")
        except Exception as e:
            print(f"ERROR: history 追加失敗: {e}", file=sys.stderr)
            return 1

    print(json.dumps({"news_id": args.news_id, "ok": True, "actions": actions}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
