#!/usr/bin/env python3
"""
初回セットアップスクリプト
ブラウザプロファイルを作成し、手動でXにログインする
"""

import sys
from pathlib import Path

# プロジェクトルートをパスに追加
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from playwright.sync_api import sync_playwright
from collector.config import PROFILE_PATH


def setup_profile():
    """
    ブラウザプロファイルを作成し、Xにログインする
    """
    profile_path = Path(PROFILE_PATH).resolve()
    profile_path.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("X投稿収集システム - 初回セットアップ")
    print("=" * 60)
    print()
    print("ブラウザが開きます。")
    print("Xにログインしてください（2FAがあれば入力）。")
    print()
    print(f"プロファイル保存先: {profile_path}")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=False,
            viewport={"width": 1280, "height": 900},
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )

        page = browser.pages[0] if browser.pages else browser.new_page()

        # Xログインページを開く
        page.goto("https://twitter.com/login")

        print("-" * 60)
        print("手動でログインしてください。")
        print("ログイン完了後、ホーム画面が表示されたら")
        print("このターミナルでEnterを押してください。")
        print("-" * 60)

        input("\n[Enter]を押して続行...")

        # ログイン状態を確認
        try:
            page.wait_for_selector(
                '[data-testid="SideNav_AccountSwitcher_Button"]',
                timeout=5000
            )
            print("\nログイン確認OK!")
        except:
            print("\n[警告] ログイン状態を確認できませんでした。")
            print("再度ログインを確認してください。")

        browser.close()

    print()
    print("=" * 60)
    print("セットアップ完了!")
    print("=" * 60)
    print()
    print("次のステップ:")
    print("  python scripts/collect_tweets.py")
    print()


if __name__ == "__main__":
    setup_profile()
