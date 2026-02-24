"""
インフルエンサーアカウントの非活動チェックモジュール

INFLUENCER_GROUPSに定義された全アカウントの状態（凍結・非公開・最終投稿日）を
Playwrightで確認し、非活動アカウントを検出する。
結果は日付ベースディレクトリにキャッシュされ、同日の再実行を高速化する。
"""
import json
import os
import time
import random
from datetime import datetime
from pathlib import Path
from typing import Optional

from collector.config import INFLUENCER_GROUPS

# 非活動判定の閾値（日数）
INACTIVE_THRESHOLD_DAYS = 30


def get_all_usernames() -> list[str]:
    """INFLUENCER_GROUPSから全ユーザー名をフラットリストで返す。

    Returns:
        全グループのユーザー名リスト
    """
    usernames = []
    for group in INFLUENCER_GROUPS.values():
        for acc in group["accounts"]:
            usernames.append(acc["username"])
    return usernames


def check_account_status(page, username: str) -> dict:
    """アカウントの状態と最終投稿日を確認する。

    Xのプロフィールページにアクセスし、アカウント状態（凍結・非公開等）と
    最新ツイートの投稿日を取得する。ピン留めツイートはスキップして判定する。

    Args:
        page: PlaywrightのPageオブジェクト
        username: 確認対象の@ユーザー名（@なし）

    Returns:
        チェック結果の辞書:
            - username: ユーザー名
            - status: "active", "not_found", "suspended", "protected", "no_tweets_found", "error"
            - last_post_date: 最終投稿日時（ISO 8601）またはNone
            - last_post_text: 最終投稿の先頭100文字またはNone
            - error: エラーメッセージまたはNone
    """
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

        # アカウント存在確認（凍結・削除のメッセージを確認）
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
            except Exception:
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


def load_cached_results(output_dir: str = "./output") -> Optional[list]:
    """同日のキャッシュ済みチェック結果を読み込む。

    日付ベースディレクトリにキャッシュファイルが存在し、かつ現在の
    INFLUENCER_GROUPSの全アカウントをカバーしている場合のみ結果を返す。
    新規アカウントが追加された場合はNoneを返し、再チェックを促す。

    Args:
        output_dir: 出力ディレクトリのパス

    Returns:
        キャッシュ済みの結果リスト、またはNone（キャッシュなし/不完全時）
    """
    date_str = datetime.now().strftime("%Y-%m-%d")
    cache_path = Path(output_dir) / date_str / "inactive_check_result.json"
    if not cache_path.exists():
        return None
    with open(cache_path, 'r', encoding='utf-8') as f:
        results = json.load(f)
    # キャッシュカバレッジチェック
    cached_usernames = {r["username"] for r in results}
    all_usernames = set(get_all_usernames())
    if not all_usernames.issubset(cached_usernames):
        return None  # 新規アカウント追加されたので再チェック必要
    return results


def save_results(results: list, output_dir: str = "./output") -> Path:
    """チェック結果をJSON形式で日付ベースディレクトリに保存する。

    Args:
        results: チェック結果のリスト
        output_dir: 出力ディレクトリのパス

    Returns:
        保存先のPathオブジェクト
    """
    date_str = datetime.now().strftime("%Y-%m-%d")
    date_dir = Path(output_dir) / date_str
    date_dir.mkdir(parents=True, exist_ok=True)
    output_path = date_dir / "inactive_check_result.json"
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"結果を保存しました: {output_path}")
    return output_path


def run_inactive_check(
    profile_path: str = "./x_profile",
    headless: bool = False,
    use_cache: bool = True,
    output_dir: str = "./output"
) -> list:
    """非活動チェックのメイン実行関数。

    キャッシュが有効な場合は同日の結果を再利用する。
    キャッシュがない場合はPlaywrightでブラウザを起動し、全アカウントを巡回して
    状態を確認する。

    Args:
        profile_path: ブラウザプロファイル（cookies.json）のディレクトリパス
        headless: ヘッドレスモードで実行するかどうか
        use_cache: 同日キャッシュを使用するかどうか
        output_dir: 出力ディレクトリのパス

    Returns:
        全アカウントのチェック結果リスト
    """
    if use_cache:
        cached = load_cached_results(output_dir)
        if cached is not None:
            print(f"同日キャッシュを使用: {len(cached)}件")
            return cached

    usernames = get_all_usernames()
    profile_path_obj = Path(profile_path)
    cookies = _load_cookies(profile_path_obj)
    if not cookies:
        print("警告: Cookieが見つかりません。ログインが必要な場合があります。")

    from playwright.sync_api import sync_playwright

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="ja-JP",
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            )
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()

            print(f"\nアカウント状態確認: {len(usernames)}件")
            for i, username in enumerate(usernames, 1):
                print(f"[{i}/{len(usernames)}] @{username} を確認中...")
                result = check_account_status(page, username)
                results.append(result)
                # 結果表示
                if result["status"] == "active":
                    date_display = result["last_post_date"][:10] if result["last_post_date"] else "不明"
                    print(f"  → アクティブ | 最終投稿: {date_display}")
                elif result["status"] == "suspended":
                    print(f"  → 凍結")
                elif result["status"] == "not_found":
                    print(f"  → アカウント不存在")
                elif result["status"] == "protected":
                    print(f"  → 非公開")
                else:
                    print(f"  → {result['status']}: {result.get('error', '')}")

                if i < len(usernames):
                    wait_time = random.uniform(3, 6)
                    time.sleep(wait_time)
        finally:
            browser.close()

    save_results(results, output_dir)
    return results


def detect_inactive_accounts(
    results: list,
    threshold_days: int = INACTIVE_THRESHOLD_DAYS
) -> set[str]:
    """チェック結果から非活動アカウントを検出する。

    非活動判定ルール:
        - not_found, suspended, error, no_tweets_found → 非活動（除外対象）
        - protected → 判断不能のため除外しない
        - active + last_post_date がthreshold_days日以上前 → 非活動
        - 日付パース失敗 → フェイルセーフで除外しない

    Args:
        results: check_account_statusの結果リスト
        threshold_days: 非活動とみなす日数の閾値

    Returns:
        非活動アカウントのユーザー名セット
    """
    inactive = set()
    today = datetime.now()

    for r in results:
        status = r.get("status", "unknown")
        username = r["username"]

        if status in ("not_found", "suspended", "error", "no_tweets_found"):
            inactive.add(username)
            continue

        if status == "protected":
            continue  # 判断不能、除外しない

        if status == "active" and r.get("last_post_date"):
            try:
                last_date = datetime.strptime(r["last_post_date"][:10], "%Y-%m-%d")
                days_since = (today - last_date).days
                if days_since >= threshold_days:
                    inactive.add(username)
            except (ValueError, TypeError):
                continue  # パース失敗はフェイルセーフ

    return inactive


def _load_cookies(profile_path: Path) -> list:
    """ブラウザプロファイルからCookieを読み込む。

    ファイルパーミッションのセキュリティチェックと、
    Cookie有効期限のチェックも行い、警告を表示する。

    Args:
        profile_path: x_profileディレクトリのPath

    Returns:
        Cookieのリスト（ファイルが存在しない場合は空リスト）
    """
    cookies_file = profile_path / "cookies.json"
    if cookies_file.exists():
        # ファイルパーミッションチェック（world-readableの場合は警告）
        file_stat = os.stat(cookies_file)
        file_mode = file_stat.st_mode
        if file_mode & 0o004:  # others-readable
            print(f"セキュリティ警告: {cookies_file} が他のユーザーから読み取り可能です。")
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
            print(f"警告: {expired_count}件の期限切れCookieが含まれています。再ログインを推奨します。")

        return cookies
    return []
