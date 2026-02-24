"""
インフルエンサーアカウントの最終投稿日確認スクリプト
collector.inactive_checkerモジュールのCLIラッパー
"""
import sys
import argparse
from pathlib import Path
from datetime import datetime

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))

from collector.inactive_checker import (
    get_all_usernames,
    run_inactive_check,
    detect_inactive_accounts,
    INACTIVE_THRESHOLD_DAYS,
)


def main():
    parser = argparse.ArgumentParser(description="インフルエンサーアカウントの最終投稿日確認")
    parser.add_argument("--headless", action="store_true", default=False,
                        help="ヘッドレスモードで実行")
    parser.add_argument("--days", type=int, default=INACTIVE_THRESHOLD_DAYS,
                        help=f"非活動判定の閾値日数 (default: {INACTIVE_THRESHOLD_DAYS})")
    parser.add_argument("--no-cache", action="store_true", default=False,
                        help="キャッシュを使わず再チェック")
    parser.add_argument("--profile", "-p", type=str, default="./x_profile",
                        help="ブラウザプロファイルのパス (default: ./x_profile)")
    parser.add_argument("--output", "-o", type=str, default="./output",
                        help="出力ディレクトリ (default: ./output)")
    args = parser.parse_args()

    usernames = get_all_usernames()
    print(f"\n{'='*60}")
    print(f"インフルエンサーアカウント状態確認")
    print(f"対象: {len(usernames)}件")
    print(f"{'='*60}\n")

    # チェック実行
    results = run_inactive_check(
        profile_path=args.profile,
        headless=args.headless,
        use_cache=not args.no_cache,
        output_dir=args.output
    )

    # 非活動アカウント検出
    inactive = detect_inactive_accounts(results, threshold_days=args.days)

    # 結果サマリー
    print(f"\n{'='*60}")
    print("結果サマリー")
    print(f"{'='*60}")

    today = datetime.now()

    # 日付でソート
    active_results = [r for r in results if r["status"] == "active" and r.get("last_post_date")]
    other_results = [r for r in results if r["status"] != "active" or not r.get("last_post_date")]

    if active_results:
        active_results.sort(key=lambda x: x["last_post_date"])

        print("\n【最終投稿日（古い順）】")
        for r in active_results:
            date_str = r["last_post_date"][:10]
            post_date = datetime.strptime(date_str, "%Y-%m-%d")
            days_ago = (today - post_date).days
            marker = " ← 非活動" if r["username"] in inactive else ""
            print(f"  @{r['username']}: {date_str} ({days_ago}日前){marker}")

    if other_results:
        print("\n【確認できなかったアカウント】")
        for r in other_results:
            marker = " ← 非活動" if r["username"] in inactive else ""
            print(f"  @{r['username']}: {r['status']}{marker}")

    if inactive:
        print(f"\n【非活動アカウント: {len(inactive)}件（閾値: {args.days}日）】")
        for username in sorted(inactive):
            print(f"  @{username}")

    print(f"\n合計: {len(results)}件中 {len(inactive)}件が非活動")


if __name__ == "__main__":
    main()
