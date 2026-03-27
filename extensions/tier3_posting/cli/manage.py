#!/usr/bin/env python3
"""PostStore CLI管理ツール。ドラフトのステータス確認・一括操作・データ圧縮を行う。"""
import argparse
import glob
import json
import os
import shutil
from collections import Counter
from datetime import datetime, timezone, timedelta

from ..x_poster.post_store import PostStore


def cmd_status(store: PostStore, args: argparse.Namespace) -> None:
    """ステータス別件数表示。"""
    drafts = store.load_drafts()
    counter = Counter(d.get("status", "unknown") for d in drafts)

    # 表示順を固定
    ordered_statuses = [
        "draft", "approved", "scheduled", "posted",
        "rejected", "failed", "archived",
    ]
    # 定義外のステータスがあれば末尾に追加
    extra = sorted(s for s in counter if s not in ordered_statuses)

    print("Draft Statistics:")
    total = 0
    for status in ordered_statuses + extra:
        count = counter.get(status, 0)
        total += count
        print(f"  {status + ':':12s} {count}")
    print(f"  {'─' * 13}")
    print(f"  {'Total:':12s} {total}")


def cmd_archive_stale(store: PostStore, args: argparse.Namespace) -> None:
    """古いdraft/approvedをアーカイブ。"""
    drafts = store.load_drafts()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    targets = []
    for d in drafts:
        if d.get("status") not in ("draft", "approved"):
            continue
        created_at_str = d.get("created_at")
        if not created_at_str:
            continue
        try:
            created_at = datetime.fromisoformat(created_at_str)
            # タイムゾーン情報がない場合はUTCとして扱う
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if created_at < cutoff:
            targets.append(d)

    if not targets:
        print(f"{args.days}日以上古いdraft/approvedはありません。")
        return

    print(f"対象: {len(targets)}件 ({args.days}日以上古いdraft/approved)")
    for d in targets:
        print(f"  [{d.get('status')}] {d.get('news_id', '?')[:40]}  created: {d.get('created_at', '?')[:19]}")

    if not args.execute:
        print("\n※ dry-run モードです。実行するには --execute を付けてください。")
        return

    archived = 0
    for d in targets:
        news_id = d.get("news_id")
        if news_id and store.archive_draft(news_id, reason="stale_data"):
            archived += 1
    print(f"\n{archived}件をアーカイブしました。")


def cmd_compact(store: PostStore, args: argparse.Namespace) -> None:
    """drafts.jsonl圧縮。_updateレコードを統合して書き直す。"""
    if not os.path.exists(store.drafts_path):
        print("drafts.jsonl が見つかりません。")
        return

    # 圧縮前の行数
    with open(store.drafts_path, "r", encoding="utf-8") as f:
        before_lines = sum(1 for line in f if line.strip())

    # マージ済みの最終状態を取得
    drafts = store.load_drafts()

    # バックアップ
    bak_path = store.drafts_path + ".bak"
    shutil.copy2(store.drafts_path, bak_path)
    print(f"バックアップ: {bak_path}")

    # 新しいJSONLに書き直し（各ドラフトが1行、_updateレコードなし）
    with open(store.drafts_path, "w", encoding="utf-8") as f:
        for d in drafts:
            # _update フラグがあれば除去
            d.pop("_update", None)
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    after_lines = len(drafts)

    # index.json も再生成
    store._index = None  # キャッシュをクリア
    store._load_index()
    store._save_index()

    print(f"圧縮完了: {before_lines}行 → {after_lines}行 (削減: {before_lines - after_lines}行)")


def cmd_cleanup_screenshots(store: PostStore, args: argparse.Namespace) -> None:
    """古いスクリーンショット削除。"""
    patterns = [
        os.path.join(store.base_dir, "schedule_error_*.png"),
        os.path.join(store.base_dir, "schedule_dryrun_*.png"),
    ]

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    targets = []

    for pattern in patterns:
        for filepath in glob.glob(pattern):
            basename = os.path.basename(filepath)
            # ファイル名からタイムスタンプ（YYYYMMDD_HHMMSS）を抽出
            # 例: schedule_error_20260301_120000.png
            parts = basename.rsplit(".", 1)[0]  # 拡張子除去
            # タイムスタンプ部分を末尾から抽出（YYYYMMDD_HHMMSS）
            tokens = parts.split("_")
            # 末尾2トークンがYYYYMMDD, HHMMSSの形式を想定
            if len(tokens) >= 3:
                try:
                    ts_str = f"{tokens[-2]}_{tokens[-1]}"
                    ts = datetime.strptime(ts_str, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        targets.append(filepath)
                except (ValueError, IndexError):
                    continue

    if not targets:
        print(f"{args.days}日以上古いスクリーンショットはありません。")
        return

    print(f"対象: {len(targets)}件 ({args.days}日以上古いスクリーンショット)")
    for fp in sorted(targets):
        print(f"  {os.path.basename(fp)}")

    if not args.execute:
        print("\n※ dry-run モードです。実行するには --execute を付けてください。")
        return

    deleted = 0
    for fp in targets:
        try:
            os.remove(fp)
            deleted += 1
        except OSError as e:
            print(f"  削除失敗: {os.path.basename(fp)} ({e})")
    print(f"\n{deleted}件を削除しました。")


def cmd_reset_failed(store: PostStore, args: argparse.Namespace) -> None:
    """failed全件リセット。"""
    failed = store.load_drafts(status_filter="failed")

    if not failed:
        print("failedステータスのドラフトはありません。")
        return

    print(f"対象: {len(failed)}件 (failed → {args.to})")
    for d in failed:
        print(f"  {d.get('news_id', '?')[:40]}  updated: {d.get('updated_at', '?')[:19]}")

    reset = 0
    for d in failed:
        news_id = d.get("news_id")
        if news_id and store.update_draft_status(news_id, args.to):
            reset += 1
    print(f"\n{reset}件を {args.to} にリセットしました。")


def main() -> None:
    parser = argparse.ArgumentParser(description="PostStore CLI管理ツール")
    parser.add_argument("--data-dir", default="output/posting", help="PostStoreディレクトリ")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    subparsers.add_parser("status", help="ステータス別件数表示")

    # archive-stale
    p_archive = subparsers.add_parser("archive-stale", help="古いドラフトをアーカイブ")
    p_archive.add_argument("--days", type=int, default=7, help="N日以上古いものを対象")
    p_archive.add_argument("--execute", action="store_true", help="実行（デフォルトはdry-run）")

    # compact
    subparsers.add_parser("compact", help="drafts.jsonl圧縮")

    # cleanup-screenshots
    p_cleanup = subparsers.add_parser("cleanup-screenshots", help="古いスクリーンショット削除")
    p_cleanup.add_argument("--days", type=int, default=30, help="N日以上古いものを対象")
    p_cleanup.add_argument("--execute", action="store_true", help="実行（デフォルトはdry-run）")

    # reset-failed
    p_reset = subparsers.add_parser("reset-failed", help="failed全件リセット")
    p_reset.add_argument("--to", default="approved", help="リセット先ステータス")

    args = parser.parse_args()
    store = PostStore(base_dir=args.data_dir)

    commands = {
        "status": cmd_status,
        "archive-stale": cmd_archive_stale,
        "compact": cmd_compact,
        "cleanup-screenshots": cmd_cleanup_screenshots,
        "reset-failed": cmd_reset_failed,
    }
    commands[args.command](store, args)


if __name__ == "__main__":
    main()
