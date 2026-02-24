#!/usr/bin/env python3
"""
データ保持ポリシー: 古い収集データの自動削除
デフォルト: 90日以上前のデータを削除
"""
import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_OUTPUT_DIR = "output"
DEFAULT_RETENTION_DAYS = 90


def cleanup_old_data(output_dir: str = DEFAULT_OUTPUT_DIR, retention_days: int = DEFAULT_RETENTION_DAYS, dry_run: bool = False):
    """保持期間を超えたデータディレクトリを削除"""
    output_path = Path(output_dir)
    if not output_path.exists():
        print(f"出力ディレクトリが見つかりません: {output_dir}")
        return 0

    cutoff = datetime.now() - timedelta(days=retention_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    removed = 0
    for entry in sorted(output_path.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        # YYYY-MM-DD 形式のディレクトリのみ対象
        try:
            datetime.strptime(entry.name, "%Y-%m-%d")
        except ValueError:
            continue

        if entry.name < cutoff_str:
            if dry_run:
                print(f"[DRY RUN] 削除対象: {entry} (保持期間 {retention_days} 日超過)")
            else:
                print(f"削除: {entry} (保持期間 {retention_days} 日超過)")
                shutil.rmtree(entry)
            removed += 1

    action = "削除対象" if dry_run else "削除済み"
    print(f"\nクリーンアップ完了: {removed} ディレクトリ {action}")
    return removed


def check_cookie_expiry(cookie_file: Path, warn_days: int = 7):
    """Cookieの有効期限をチェックし、期限切れが近い場合に警告"""
    cookie_file = Path(cookie_file)
    if not cookie_file.exists():
        print(f"Cookieファイルが見つかりません: {cookie_file}")
        return

    try:
        with open(cookie_file, "r") as f:
            cookies = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # 暗号化されたCookieの場合はスキップ
        print("Cookieファイルは暗号化されています（有効期限チェックをスキップ）")
        return

    now = datetime.now().timestamp()
    expiring = 0
    for cookie in cookies:
        expires = cookie.get("expires", 0)
        if expires > 0 and expires - now < warn_days * 86400:
            days_left = max(0, int((expires - now) / 86400))
            print(f"[警告] Cookie '{cookie.get('name', 'unknown')}' の有効期限が{days_left}日後に切れます")
            expiring += 1

    if expiring == 0:
        print("全Cookieの有効期限は正常です")


def main():
    parser = argparse.ArgumentParser(description="データ保持ポリシー管理")
    parser.add_argument("--output", "-o", default=DEFAULT_OUTPUT_DIR, help="出力ディレクトリ")
    parser.add_argument("--days", "-d", type=int, default=DEFAULT_RETENTION_DAYS, help="保持日数 (デフォルト: 90)")
    parser.add_argument("--dry-run", action="store_true", help="削除せず対象のみ表示")
    parser.add_argument("--check-cookies", action="store_true", help="Cookie有効期限チェック")
    parser.add_argument("--cookie-file", default="x_profile/cookies.json", help="Cookieファイルパス")
    args = parser.parse_args()

    print("=" * 50)
    print("データ保持ポリシー管理")
    print("=" * 50)

    if args.check_cookies:
        print("\n--- Cookie有効期限チェック ---")
        check_cookie_expiry(args.cookie_file)

    print(f"\n--- データクリーンアップ (保持期間: {args.days}日) ---")
    cleanup_old_data(args.output, args.days, args.dry_run)


if __name__ == "__main__":
    main()
