"""
X投稿収集クラス
安全なブラウザ自動化による投稿収集
"""

from playwright.sync_api import sync_playwright, Page, BrowserContext
import time
import random
import json
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

from .config import COLLECTION_SETTINGS, PROFILE_PATH, OUTPUT_DIR


class SafeXCollector:
    """
    安全なX投稿収集クラス

    特徴:
    - 手動ログイン済みプロファイルを使用
    - 人間らしいアクセスパターン
    - 週1-2回程度の使用を想定
    """

    def __init__(self, profile_path: str = PROFILE_PATH):
        """
        Args:
            profile_path: ログイン済みブラウザプロファイルのパス
        """
        self.profile_path = Path(profile_path).resolve()
        self.tweets: List[Dict] = []
        self.collected_urls: set = set()  # 重複防止用
        self.settings = COLLECTION_SETTINGS

    def _load_cookies(self) -> List[Dict]:
        """保存済みCookieを読み込む"""
        cookie_file = self.profile_path / "cookies.json"
        if cookie_file.exists():
            with open(cookie_file, 'r') as f:
                return json.load(f)
        return []

    def collect(
        self,
        search_url: str,
        max_scrolls: int = None,
        group_name: str = "unknown",
        stop_after_empty: int = 3
    ) -> List[Dict]:
        """
        検索URLから投稿を収集（動的終了判定付き）

        Args:
            search_url: X検索URL
            max_scrolls: 最大スクロール回数（省略時は設定値を使用）
            group_name: グループ名（ログ用）
            stop_after_empty: 新規0件が連続N回で終了（デフォルト3）

        Returns:
            収集したツイートのリスト
        """
        if max_scrolls is None:
            max_scrolls = self.settings["max_scrolls"]

        self.tweets = []
        self.collected_urls = set()

        print(f"\n{'='*60}")
        print(f"収集開始: {group_name}")
        print(f"URL: {search_url[:80]}...")
        print(f"最大スクロール: {max_scrolls}回")
        print(f"動的終了: 新規0件が{stop_after_empty}回連続で終了")
        print(f"{'='*60}\n")

        # Cookieファイルを確認
        cookies = self._load_cookies()
        if cookies:
            print(f"保存済みCookieを読み込みました ({len(cookies)}個)")

        with sync_playwright() as p:
            # 通常のブラウザを起動してCookieを適用
            browser = p.chromium.launch(
                headless=False,  # 必ず画面表示
            )

            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
            )

            # Cookieを適用
            if cookies:
                context.add_cookies(cookies)

            try:
                page = context.new_page()

                # ページ遷移
                print("ページを開いています...")
                page.goto(search_url, wait_until="domcontentloaded")

                # 初期読み込み待機
                self._human_wait(3, 5)

                # ログイン状態確認
                if not self._check_login_status(page):
                    print("\n[警告] ログインしていない可能性があります")
                    print("scripts/setup_profile.py を実行してログインしてください")
                    return []

                print("ログイン確認OK\n")

                # 動的終了判定用カウンター
                consecutive_empty = 0

                # スクロールして投稿を収集
                for i in range(max_scrolls):
                    print(f"スクロール {i+1}/{max_scrolls} ", end="")

                    # 現在表示されているツイートを収集
                    new_count = self._collect_visible_tweets(page)
                    print(f"(+{new_count}件, 合計{len(self.tweets)}件)", end="")

                    # 動的終了判定
                    if new_count == 0:
                        consecutive_empty += 1
                        print(f" [空{consecutive_empty}/{stop_after_empty}]", end="")
                        if consecutive_empty >= stop_after_empty:
                            print(f"\n\n[終了] 新規0件が{stop_after_empty}回連続のため収集終了")
                            break
                    else:
                        consecutive_empty = 0  # リセット

                    print()  # 改行

                    # 最後のスクロールでなければ待機
                    if i < max_scrolls - 1:
                        # 人間らしいスクロール
                        self._human_scroll(page)

                        # 人間らしい待機
                        self._human_wait(
                            self.settings["min_wait_sec"],
                            self.settings["max_wait_sec"]
                        )

                        # 途中で読むような動作
                        if random.random() < self.settings["reading_probability"]:
                            self._simulate_reading(page)

            except Exception as e:
                print(f"\n[エラー] 収集中にエラーが発生: {e}")

            finally:
                context.close()
                browser.close()

        print(f"\n収集完了: {len(self.tweets)}件")
        return self.tweets

    def _check_login_status(self, page: Page) -> bool:
        """ログイン状態を確認"""
        try:
            # ログインしていればホームリンクやアカウントメニューが存在
            page.wait_for_selector(
                '[data-testid="SideNav_AccountSwitcher_Button"], [data-testid="AppTabBar_Home_Link"]',
                timeout=5000
            )
            return True
        except:
            return False

    def _human_wait(self, min_sec: float, max_sec: float):
        """人間らしいランダム待機"""
        wait_time = random.uniform(min_sec, max_sec)
        time.sleep(wait_time)

    def _human_scroll(self, page: Page):
        """人間らしいスクロール"""
        scroll_amount = random.randint(
            self.settings["scroll_min"],
            self.settings["scroll_max"]
        )

        # 滑らかにスクロール
        steps = random.randint(3, 6)
        for _ in range(steps):
            page.mouse.wheel(0, scroll_amount // steps)
            time.sleep(random.uniform(0.1, 0.3))

    def _simulate_reading(self, page: Page):
        """記事を読んでいるような動作"""
        print("  (読み込み中...)", end="", flush=True)

        # 長めの停止
        time.sleep(random.uniform(
            self.settings["reading_min_sec"],
            self.settings["reading_max_sec"]
        ))

        # マウスを動かす
        page.mouse.move(
            random.randint(100, 800),
            random.randint(100, 600)
        )
        print(" 完了")

    def _collect_visible_tweets(self, page: Page) -> int:
        """
        表示中のツイートを収集

        Returns:
            新規収集件数
        """
        new_count = 0

        try:
            tweet_cards = page.query_selector_all('[data-testid="tweet"]')

            for card in tweet_cards:
                tweet_data = self._parse_tweet_card(card)

                if tweet_data and tweet_data['url'] not in self.collected_urls:
                    self.tweets.append(tweet_data)
                    self.collected_urls.add(tweet_data['url'])
                    new_count += 1

        except Exception as e:
            print(f"\n  [警告] ツイート収集中にエラー: {e}")

        return new_count

    def _parse_tweet_card(self, card) -> Optional[Dict]:
        """
        ツイートカードをパース

        Returns:
            パース結果の辞書、失敗時はNone
        """
        try:
            # ユーザー名
            user_elem = card.query_selector('[data-testid="User-Name"]')
            username_text = user_elem.inner_text() if user_elem else ""

            # @usernameを抽出
            username_match = re.search(r'@(\w+)', username_text)
            username = username_match.group(1) if username_match else ""

            # 表示名（@の前の部分）
            display_name = username_text.split('@')[0].strip() if '@' in username_text else username_text

            # ツイート本文
            text_elem = card.query_selector('[data-testid="tweetText"]')
            text = text_elem.inner_text() if text_elem else ""

            # ツイートURL
            link_elems = card.query_selector_all('a[href*="/status/"]')
            tweet_url = ""
            for link in link_elems:
                href = link.get_attribute('href')
                if href and '/status/' in href:
                    tweet_url = href
                    break

            if not tweet_url:
                return None

            # フルURLに変換
            if tweet_url.startswith('/'):
                tweet_url = f"https://twitter.com{tweet_url}"

            # 投稿時間（あれば）
            time_elem = card.query_selector('time')
            posted_at = time_elem.get_attribute('datetime') if time_elem else None

            # いいね数（表示されていれば）
            like_elem = card.query_selector('[data-testid="like"] span span')
            like_count = None
            if like_elem:
                like_text = like_elem.inner_text()
                like_count = self._parse_count(like_text)

            # リツイート数
            retweet_elem = card.query_selector('[data-testid="retweet"] span span')
            retweet_count = None
            if retweet_elem:
                retweet_text = retweet_elem.inner_text()
                retweet_count = self._parse_count(retweet_text)

            return {
                'username': username,
                'display_name': display_name,
                'text': text,
                'url': tweet_url,
                'posted_at': posted_at,
                'like_count': like_count,
                'retweet_count': retweet_count,
                'collected_at': datetime.now().isoformat()
            }

        except Exception as e:
            return None

    def _parse_count(self, text: str) -> Optional[int]:
        """
        数値文字列をパース（1.2K, 2.3M など対応）
        """
        if not text:
            return None

        text = text.strip().upper()

        try:
            if 'K' in text:
                return int(float(text.replace('K', '')) * 1000)
            elif 'M' in text:
                return int(float(text.replace('M', '')) * 1000000)
            else:
                # カンマ除去
                return int(text.replace(',', ''))
        except:
            return None

    def save_to_json(self, filename: str = None, output_dir: str = OUTPUT_DIR):
        """
        結果をJSONで保存

        Args:
            filename: ファイル名（省略時は日時から自動生成）
            output_dir: 出力ディレクトリ
        """
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"tweets_{timestamp}.json"

        filepath = output_path / filename

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.tweets, f, ensure_ascii=False, indent=2)

        print(f"保存完了: {filepath} ({len(self.tweets)}件)")
        return filepath

    def save_to_csv(self, filename: str = None, output_dir: str = OUTPUT_DIR):
        """
        結果をCSVで保存

        Args:
            filename: ファイル名（省略時は日時から自動生成）
            output_dir: 出力ディレクトリ
        """
        import csv

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"tweets_{timestamp}.csv"

        filepath = output_path / filename

        if not self.tweets:
            print("保存するツイートがありません")
            return None

        fieldnames = list(self.tweets[0].keys())

        with open(filepath, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self.tweets)

        print(f"保存完了: {filepath} ({len(self.tweets)}件)")
        return filepath


def collect_all_groups(
    groups: List[str] = None,
    max_scrolls: int = 10,
    profile_path: str = PROFILE_PATH
) -> Dict[str, List[Dict]]:
    """
    複数グループをまとめて収集

    Args:
        groups: 収集するグループ名のリスト（省略時は全グループ）
        max_scrolls: 各グループのスクロール回数
        profile_path: プロファイルパス

    Returns:
        グループ名をキーとした収集結果
    """
    from .config import SEARCH_URLS, INFLUENCER_GROUPS

    if groups is None:
        groups = list(SEARCH_URLS.keys())

    collector = SafeXCollector(profile_path=profile_path)
    results = {}

    for group_key in groups:
        if group_key not in SEARCH_URLS:
            print(f"[警告] 不明なグループ: {group_key}")
            continue

        group_info = INFLUENCER_GROUPS.get(group_key, {})
        group_name = group_info.get('name', group_key)

        search_url = SEARCH_URLS[group_key]

        tweets = collector.collect(
            search_url=search_url,
            max_scrolls=max_scrolls,
            group_name=f"{group_key} ({group_name})"
        )

        # グループ情報を各ツイートに追加
        for tweet in tweets:
            tweet['group'] = group_key
            tweet['is_contrarian'] = group_info.get('is_contrarian', False)

        results[group_key] = tweets

        # グループ間で長めの待機
        if group_key != groups[-1]:
            wait_time = random.uniform(30, 60)
            print(f"\n次のグループまで {wait_time:.0f}秒 待機...")
            time.sleep(wait_time)

    return results
