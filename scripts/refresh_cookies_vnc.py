#!/usr/bin/env python3
"""VNC経由Cookie再取得スクリプト（非対話型）

VNCコンテナ内で実行。ブラウザを開き、ログイン完了を自動検知してCookieを保存する。
Usage:
    docker exec xstock-vnc python scripts/refresh_cookies_vnc.py [--timeout 300]
"""

import argparse
import json
import sys
import time
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from playwright.sync_api import sync_playwright
from collector.config import PROFILE_PATH


def main():
    parser = argparse.ArgumentParser(description="VNC経由Cookie再取得")
    parser.add_argument("--timeout", type=int, default=300, help="ログイン待機タイムアウト（秒、デフォルト: 300）")
    parser.add_argument("--account", default=None, help="アカウントID（x_profiles/<account>/に保存）")
    args = parser.parse_args()

    if args.account:
        profile_path = (project_root / "x_profiles" / args.account).resolve()
    else:
        profile_path = Path(PROFILE_PATH).resolve()
    profile_path.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Cookie再取得 - VNC経由（非対話型）")
    print("=" * 60)
    print()
    print("ブラウザでXログインページを開きます。")
    print("http://localhost:6080 のVNCブラウザでログインしてください。")
    print(f"タイムアウト: {args.timeout}秒")
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

        # Check if already logged in
        page.goto("https://twitter.com/home", wait_until="domcontentloaded")
        time.sleep(3)

        logged_in = False
        try:
            page.wait_for_selector(
                '[data-testid="SideNav_AccountSwitcher_Button"], [data-testid="AppTabBar_Home_Link"]',
                timeout=5000,
            )
            logged_in = True
            print("既にログイン済みです。Cookieを更新します。")
        except Exception:
            print("未ログイン。ログインページに遷移します...")
            page.goto("https://twitter.com/login", wait_until="domcontentloaded")

        if not logged_in:
            # Poll for login completion
            print(f"\nVNCブラウザでログインしてください（最大{args.timeout}秒待機）...")
            start = time.time()
            while time.time() - start < args.timeout:
                try:
                    page.wait_for_selector(
                        '[data-testid="SideNav_AccountSwitcher_Button"], [data-testid="AppTabBar_Home_Link"]',
                        timeout=10000,
                    )
                    logged_in = True
                    print("\nログインを検知しました！")
                    break
                except Exception:
                    elapsed = int(time.time() - start)
                    print(f"  待機中... ({elapsed}/{args.timeout}秒)")

            if not logged_in:
                print("\nタイムアウト: ログインが完了しませんでした。")
                browser.close()
                sys.exit(1)

        # Save cookies
        cookies = browser.cookies()

        cookie_file = profile_path / "cookies.json"
        try:
            from collector.cookie_crypto import save_cookies_encrypted
            save_cookies_encrypted(cookies, cookie_file)
            print(f"暗号化Cookieを保存: {cookie_file}")
        except ImportError:
            with open(cookie_file, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            print(f"Cookieを保存: {cookie_file}")

        print(f"Cookie数: {len(cookies)}")
        x_cookies = [c for c in cookies if "twitter.com" in c.get("domain", "") or "x.com" in c.get("domain", "")]
        print(f"X関連Cookie数: {len(x_cookies)}")

        browser.close()

    print()
    print("=" * 60)
    print("Cookie再取得完了!")
    print("=" * 60)


if __name__ == "__main__":
    main()
