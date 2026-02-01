#!/usr/bin/env python3
"""
リモートデバッグ経由でChromeに接続するセットアップ
既存のログイン状態を完全に引き継ぐ
"""

import sys
import json
import time
from pathlib import Path

# プロジェクトルートをパスに追加
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from playwright.sync_api import sync_playwright
from collector.config import PROFILE_PATH


def setup_with_remote_chrome():
    """
    リモートデバッグ経由でChromeに接続し、Cookieを取得
    """
    print("=" * 60)
    print("X投稿収集システム - リモートChrome接続セットアップ")
    print("=" * 60)
    print()
    print("【事前準備】")
    print("1. 現在開いているChromeをすべて閉じる")
    print("2. 以下のコマンドでChromeを起動:")
    print()
    print("  macOS:")
    print('  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222')
    print()
    print("3. 開いたChromeでXにログインする")
    print("4. ログイン完了後、このスクリプトを再実行")
    print()
    print("-" * 60)

    input("準備ができたらEnterを押してください...")

    profile_path = Path(PROFILE_PATH).resolve()
    profile_path.mkdir(parents=True, exist_ok=True)

    print()
    print("Chromeに接続中...")

    try:
        with sync_playwright() as p:
            # リモートデバッグポートに接続
            browser = p.chromium.connect_over_cdp("http://localhost:9222")

            # 既存のコンテキストを取得
            contexts = browser.contexts
            if not contexts:
                print("[エラー] ブラウザコンテキストが見つかりません")
                return

            context = contexts[0]
            pages = context.pages

            print(f"接続成功! {len(pages)}個のタブを検出")

            # Xのページを探すか、新しいタブで開く
            x_page = None
            for page in pages:
                if "twitter.com" in page.url or "x.com" in page.url:
                    x_page = page
                    break

            if not x_page:
                print("Xのタブが見つかりません。新しいタブで開きます...")
                x_page = context.new_page()
                x_page.goto("https://twitter.com/home")
                time.sleep(3)

            # ログイン状態を確認
            current_url = x_page.url
            print(f"現在のURL: {current_url}")

            # Cookieを取得
            cookies = context.cookies()
            x_cookies = [c for c in cookies if "twitter.com" in c.get("domain", "") or "x.com" in c.get("domain", "")]

            if x_cookies:
                print(f"\nX関連のCookieを{len(x_cookies)}個取得しました")

                # Cookieを保存
                cookie_file = profile_path / "cookies.json"
                with open(cookie_file, "w") as f:
                    json.dump(x_cookies, f, indent=2)

                print(f"Cookieを保存しました: {cookie_file}")

                # 認証トークンの確認
                auth_token = next((c for c in x_cookies if c["name"] == "auth_token"), None)
                if auth_token:
                    print("\n[成功] 認証トークンを確認しました!")
                else:
                    print("\n[警告] 認証トークンが見つかりません。ログイン状態を確認してください。")
            else:
                print("\n[エラー] X関連のCookieが見つかりません")
                print("Chromeでhttps://twitter.comにアクセスしてログインしてください")

            # ブラウザは閉じない（ユーザーのChromeなので）
            browser.close()

    except Exception as e:
        if "Connection refused" in str(e) or "connect" in str(e).lower():
            print()
            print("[エラー] Chromeに接続できません")
            print()
            print("以下を確認してください:")
            print("1. Chromeが --remote-debugging-port=9222 で起動しているか")
            print("2. 他のChromeインスタンスが動作していないか")
            print()
            print("Chromeの起動コマンド:")
            print('/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222')
        else:
            print(f"[エラー] {e}")
        return

    print()
    print("=" * 60)
    print("セットアップ完了!")
    print("=" * 60)
    print()
    print("次のステップ:")
    print("  python scripts/collect_tweets.py --groups group1 --scrolls 3")
    print()


if __name__ == "__main__":
    setup_with_remote_chrome()
