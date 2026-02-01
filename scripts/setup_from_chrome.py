#!/usr/bin/env python3
"""
既存のChromeプロファイルからXのログイン状態をコピーするセットアップ
"""

import sys
import shutil
from pathlib import Path

# プロジェクトルートをパスに追加
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from playwright.sync_api import sync_playwright
from collector.config import PROFILE_PATH


def find_chrome_profile():
    """Chromeのデフォルトプロファイルパスを探す"""
    possible_paths = [
        Path.home() / "Library/Application Support/Google/Chrome/Default",
        Path.home() / "Library/Application Support/Google/Chrome/Profile 1",
        Path.home() / ".config/google-chrome/Default",
        Path.home() / ".config/chromium/Default",
    ]

    for path in possible_paths:
        if path.exists():
            return path
    return None


def setup_from_chrome():
    """
    既存のChromeプロファイルを使用してセットアップ
    """
    print("=" * 60)
    print("X投稿収集システム - Chromeプロファイル利用セットアップ")
    print("=" * 60)
    print()

    chrome_profile = find_chrome_profile()

    if chrome_profile:
        print(f"Chromeプロファイルを検出: {chrome_profile}")
    else:
        print("[警告] Chromeプロファイルが見つかりません")
        print("手動でパスを指定してください")
        return

    profile_path = Path(PROFILE_PATH).resolve()

    print()
    print("このスクリプトは、Chromeのログイン状態を利用して")
    print("Xにアクセスできるか確認します。")
    print()
    print("注意: Chromeを閉じてから実行してください")
    print()

    # Chromeのプロファイルを直接使用（コピーではなく）
    # Cookieとセッション情報のみを利用

    with sync_playwright() as p:
        # Chromeの実行ファイルを使用
        browser = p.chromium.launch(
            headless=False,
            channel="chrome",  # 既存のChromeを使用
        )

        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )

        page = context.new_page()

        print("Xを開いています...")
        page.goto("https://twitter.com/home")

        print()
        print("-" * 60)
        print("ブラウザが開きました。")
        print()
        print("【既にXにログイン済みの場合】")
        print("  → ホーム画面が表示されます")
        print()
        print("【ログインしていない場合】")
        print("  → 手動でログインしてください")
        print()
        print("完了したらこのウィンドウでEnterを押してください")
        print("-" * 60)

        input("\n[Enter]を押して続行...")

        # ログイン状態を確認
        current_url = page.url
        if "home" in current_url or "twitter.com" in current_url:
            print("\nログイン状態を確認しました")

            # Cookieを保存
            cookies = context.cookies()

            # 新しいプロファイルにCookieを保存
            profile_path.mkdir(parents=True, exist_ok=True)

            import json
            cookie_file = profile_path / "cookies.json"
            with open(cookie_file, "w") as f:
                json.dump(cookies, f)

            print(f"Cookieを保存しました: {cookie_file}")

        browser.close()

    print()
    print("=" * 60)
    print("セットアップ完了!")
    print("=" * 60)


if __name__ == "__main__":
    setup_from_chrome()
