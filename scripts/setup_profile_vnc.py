#!/usr/bin/env python3
"""
VNC版 初回セットアップスクリプト
ブラウザを開いてXにログインする（対話入力なし）
"""

import sys
import time
from pathlib import Path

# プロジェクトルートをパスに追加
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from playwright.sync_api import sync_playwright
from collector.config import PROFILE_PATH


def setup_profile():
    """
    ブラウザプロファイルを作成し、Xログインページを開く
    """
    profile_path = Path(PROFILE_PATH).resolve()
    profile_path.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("X投稿収集システム - 初回セットアップ (VNC版)")
    print("=" * 60)
    print()
    print("ブラウザが開きます。")
    print("VNC画面でXにログインしてください。")
    print()
    print(f"プロファイル保存先: {profile_path}")
    print()
    print("ログイン完了後、ブラウザを閉じてください。")
    print("（ブラウザを閉じるとセットアップが完了します）")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=False,
            viewport={"width": 1200, "height": 800},
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )

        page = browser.pages[0] if browser.pages else browser.new_page()

        # Xログインページを開く
        print("Xログインページを開いています...")
        page.goto("https://twitter.com/login")

        print("-" * 60)
        print("VNC画面でXにログインしてください。")
        print("ログイン完了後、ブラウザのウィンドウを閉じてください。")
        print("-" * 60)

        # ブラウザが閉じられるまで待機
        try:
            while len(browser.pages) > 0:
                time.sleep(1)
        except:
            pass

        browser.close()

    print()
    print("=" * 60)
    print("セットアップ完了!")
    print("=" * 60)
    print()
    print("次のステップ:")
    print("  ツイート収集を実行するには:")
    print("  docker exec xstock-vnc python scripts/collect_tweets.py")
    print()


if __name__ == "__main__":
    setup_profile()
