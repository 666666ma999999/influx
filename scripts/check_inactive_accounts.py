"""
インフルエンサーアカウントの最終投稿日確認スクリプト
収集データに含まれていないアカウントの状態を確認する
"""
import json
import sys
import os
import argparse
from pathlib import Path
from datetime import datetime
import time
import random

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

# 確認対象アカウント
TARGET_ACCOUNTS = [
    "tesuta001",
    "cissan_9984",
    "heihachiro888",
    "kakatothecat",
    "Toushi_kensh",
    "Kosukeitou",
    "uehara_sato4",
    "pay_cashless",
    "haru_tachibana8",
    "piya00piya",
    "w_coast_0330",
    "shikiho_10"
]

# Cookie読み込み
def load_cookies(profile_path: Path) -> list:
    cookies_file = profile_path / "cookies.json"
    if cookies_file.exists():
        # ファイルパーミッションチェック（world-readableの場合は警告）
        file_stat = os.stat(cookies_file)
        file_mode = file_stat.st_mode
        if file_mode & 0o004:  # others-readable
            print(f"⚠️ セキュリティ警告: {cookies_file} が他のユーザーから読み取り可能です。")
            print(f"  推奨: chmod 600 {cookies_file}")

        with open(cookies_file, 'r') as f:
            cookies = json.load(f)

        # Cookie有効期限チェック
        now_timestamp = time.time()
        expired_count = 0
        for cookie in cookies:
            expiry = cookie.get("expires", -1)
            if expiry > 0 and expiry < now_timestamp:
                expired_count += 1
        if expired_count > 0:
            print(f"⚠️ 警告: {expired_count}件の期限切れCookieが含まれています。再ログインを推奨します。")

        return cookies
    return []


def check_account_status(page, username: str) -> dict:
    """アカウントの状態と最終投稿日を確認"""
    result = {
        "username": username,
        "status": "unknown",
        "last_post_date": None,
        "last_post_text": None,
        "error": None
    }

    try:
        url = f"https://x.com/{username}"
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(random.uniform(3, 5))

        # アカウント存在確認
        # 凍結・削除の場合のメッセージを確認
        page_content = page.content()

        if "このアカウントは存在しません" in page_content or "This account doesn't exist" in page_content:
            result["status"] = "not_found"
            return result

        if "アカウントは凍結されています" in page_content or "Account suspended" in page_content:
            result["status"] = "suspended"
            return result

        if "このアカウントの投稿は非公開です" in page_content or "These posts are protected" in page_content:
            result["status"] = "protected"
            return result

        # 最新の投稿を探す（プロフィールページのタイムライン）
        # 少し待ってからツイートを探す
        time.sleep(2)

        # ピン留めされたツイートをスキップして、最新のツイートを取得
        tweets = page.query_selector_all('[data-testid="tweet"]')

        # 全ツイートの日付を収集し、最新のものを特定
        tweet_data = []
        for tweet in tweets[:10]:  # 最初の10件をチェック
            try:
                # ピン留めかどうか確認（複数の方法で検出）
                tweet_html = tweet.inner_html()
                is_pinned = (
                    "ピン留め" in tweet_html or
                    "Pinned" in tweet_html or
                    "pinned" in tweet_html.lower()
                )

                time_elem = tweet.query_selector('time')
                if time_elem:
                    posted_at = time_elem.get_attribute('datetime')
                    text_elem = tweet.query_selector('[data-testid="tweetText"]')
                    text = text_elem.inner_text()[:100] if text_elem else ""

                    tweet_data.append({
                        "posted_at": posted_at,
                        "text": text,
                        "is_pinned": is_pinned
                    })
            except:
                continue

        # ピン留めでないツイートを日付順でソートし、最新を取得
        non_pinned = [t for t in tweet_data if not t["is_pinned"]]
        if non_pinned:
            non_pinned.sort(key=lambda x: x["posted_at"], reverse=True)
            latest = non_pinned[0]
            result["status"] = "active"
            result["last_post_date"] = latest["posted_at"]
            result["last_post_text"] = latest["text"]
            return result

        # ピン留めしかない場合、全ツイートから最新を取得
        if tweet_data:
            tweet_data.sort(key=lambda x: x["posted_at"], reverse=True)
            latest = tweet_data[0]
            result["status"] = "active"
            result["last_post_date"] = latest["posted_at"]
            result["last_post_text"] = latest["text"]
            result["note"] = "pinned_only"
            return result

        result["status"] = "no_tweets_found"

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)

    return result


def main():
    parser = argparse.ArgumentParser(description="インフルエンサーアカウントの最終投稿日確認")
    parser.add_argument("--headless", action="store_true", default=False,
                        help="ヘッドレスモードで実行（CI/サーバー環境向け）")
    args = parser.parse_args()

    profile_path = Path(__file__).parent.parent / "x_profile"
    cookies = load_cookies(profile_path)

    if not cookies:
        print("警告: Cookieが見つかりません。ログインが必要な場合があります。")

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="ja-JP",
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            )

            if cookies:
                context.add_cookies(cookies)

            page = context.new_page()

            print(f"\n{'='*60}")
            print(f"インフルエンサーアカウント状態確認")
            print(f"対象: {len(TARGET_ACCOUNTS)}件")
            print(f"{'='*60}\n")

            for i, username in enumerate(TARGET_ACCOUNTS, 1):
                print(f"[{i}/{len(TARGET_ACCOUNTS)}] @{username} を確認中...")

                result = check_account_status(page, username)
                results.append(result)

                # 結果表示
                if result["status"] == "active":
                    date_str = result["last_post_date"][:10] if result["last_post_date"] else "不明"
                    print(f"  → アクティブ | 最終投稿: {date_str}")
                elif result["status"] == "suspended":
                    print(f"  → ⚠️ 凍結")
                elif result["status"] == "not_found":
                    print(f"  → ❌ アカウント存在しない")
                elif result["status"] == "protected":
                    print(f"  → 🔒 非公開")
                else:
                    print(f"  → ？ {result['status']}: {result.get('error', '')}")

                # 人間らしい待機
                if i < len(TARGET_ACCOUNTS):
                    wait_time = random.uniform(3, 6)
                    time.sleep(wait_time)
        finally:
            browser.close()

    # 結果サマリー
    print(f"\n{'='*60}")
    print("結果サマリー")
    print(f"{'='*60}")

    today = datetime.now()

    # 日付でソート
    active_results = [r for r in results if r["status"] == "active" and r["last_post_date"]]
    inactive_results = [r for r in results if r["status"] != "active" or not r["last_post_date"]]

    if active_results:
        active_results.sort(key=lambda x: x["last_post_date"])

        print("\n【最終投稿日（古い順）】")
        for r in active_results:
            date_str = r["last_post_date"][:10]
            post_date = datetime.strptime(date_str, "%Y-%m-%d")
            days_ago = (today - post_date).days
            print(f"  @{r['username']}: {date_str} ({days_ago}日前)")

    if inactive_results:
        print("\n【確認できなかったアカウント】")
        for r in inactive_results:
            print(f"  @{r['username']}: {r['status']}")

    # 結果をJSONで保存
    output_path = Path(__file__).parent.parent / "output" / "inactive_check_result.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n結果を保存しました: {output_path}")


if __name__ == "__main__":
    main()
