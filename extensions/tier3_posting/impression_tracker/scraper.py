"""投稿済みツイートのインプレッション追跡スクレイパー。

SafeXCollector / XPosterのCookie認証・人間らしい操作パターンを踏襲。
"""

import logging
import random
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


class ImpressionScraper:
    """投稿済みツイートのインプレッション（Views/Likes/RT/Reply/Bookmark）をスクレイピング。

    Attributes:
        profile_path: ログイン済みブラウザプロファイルのパス
    """

    def __init__(self, profile_path: str = "./x_profile") -> None:
        self.profile_path = Path(profile_path).resolve()

    def scrape(self, tweet_url: str) -> Dict[str, Any]:
        """ツイートのエンゲージメント指標を取得。

        Args:
            tweet_url: ツイートURL (https://twitter.com/.../status/...)

        Returns:
            dict: {
                "tweet_url": str,
                "impressions": int,
                "likes": int,
                "retweets": int,
                "replies": int,
                "bookmarks": int,
                "engagement_rate": float,
                "scraped_at": str  # ISO 8601 JST
            }
            エラー時: {"error": str}
        """
        from playwright.sync_api import sync_playwright

        cookies = self._load_cookies()
        if not cookies:
            return {
                "error": "Cookie読込失敗: cookies.jsonが見つからないか空です",
            }

        browser = None
        context = None
        pw = None
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
            )
            context.add_cookies(cookies)

            page = context.new_page()

            # ツイートページに遷移
            page.goto(tweet_url, wait_until="domcontentloaded")
            self._human_wait(2.0, 4.0)

            # ツイート本文の読み込み待ち
            try:
                page.wait_for_selector(
                    '[data-testid="tweetText"]', timeout=15000
                )
            except Exception:
                logger.warning(
                    "ツイート本文が見つかりません: %s", tweet_url
                )
                self._take_error_screenshot(page)
                return {"error": f"ツイート本文が見つかりません: {tweet_url}"}

            self._human_wait(1.0, 2.0)

            # エンゲージメント指標の取得
            impressions = self._scrape_impressions(page)
            likes = self._scrape_metric(page, "like")
            retweets = self._scrape_metric(page, "retweet")
            replies = self._scrape_metric(page, "reply")
            bookmarks = self._scrape_metric(page, "bookmark")

            # エンゲージメント率の算出
            engagement_rate = (
                (likes + retweets + replies) / max(impressions, 1)
            )

            result = {
                "tweet_url": tweet_url,
                "impressions": impressions,
                "likes": likes,
                "retweets": retweets,
                "replies": replies,
                "bookmarks": bookmarks,
                "engagement_rate": round(engagement_rate, 6),
                "scraped_at": datetime.now(JST).isoformat(),
            }

            logger.info(
                "インプレッション取得成功: %s (views=%d, likes=%d, RT=%d)",
                tweet_url,
                impressions,
                likes,
                retweets,
            )
            return result

        except Exception as exc:
            logger.exception("インプレッション取得中にエラーが発生: %s", tweet_url)
            try:
                if page:
                    self._take_error_screenshot(page)
            except Exception:
                pass
            return {"error": str(exc)}

        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if pw:
                try:
                    pw.stop()
                except Exception:
                    pass

    def _load_cookies(self) -> list:
        """x_profile/cookies.jsonからCookie読込。

        Returns:
            Playwright形式のCookieリスト
        """
        cookie_file = self.profile_path / "cookies.json"
        try:
            from collector.cookie_crypto import load_cookies_encrypted

            return load_cookies_encrypted(cookie_file)
        except Exception as exc:
            logger.warning("Cookie読込エラー: %s", exc)
            return []

    def _scrape_impressions(self, page) -> int:
        """ツイートのインプレッション(Views)数を取得。

        Args:
            page: Playwrightのページオブジェクト

        Returns:
            インプレッション数（取得不可の場合は0）
        """
        # 方法1: analyticsリンクのテキスト
        try:
            analytics_link = page.query_selector('a[href$="/analytics"]')
            if analytics_link:
                text = analytics_link.text_content() or ""
                num = self._parse_metric_text(text)
                if num > 0:
                    return num
        except Exception:
            pass

        # 方法2: aria-label に "view" または "表示" を含む要素
        for pattern in ['[aria-label*="view"]', '[aria-label*="表示"]']:
            try:
                el = page.query_selector(pattern)
                if el:
                    label = el.get_attribute("aria-label") or ""
                    num = self._extract_number_from_label(label)
                    if num > 0:
                        return num
            except Exception:
                pass

        # 方法3: analytics リンク周辺のテキストを幅広く検索
        try:
            analytics_links = page.query_selector_all('a[href*="/analytics"]')
            for link in analytics_links:
                text = link.text_content() or ""
                num = self._parse_metric_text(text)
                if num > 0:
                    return num
        except Exception:
            pass

        logger.debug("インプレッション数を取得できませんでした")
        return 0

    def _scrape_metric(self, page, testid: str) -> int:
        """data-testid属性を使ってエンゲージメント指標を取得。

        Args:
            page: Playwrightのページオブジェクト
            testid: data-testid値 ("like", "retweet", "reply", "bookmark")

        Returns:
            指標の数値（取得不可の場合は0）
        """
        # 方法1: button[data-testid] 内の [dir="ltr"] テキスト
        try:
            selector = f'button[data-testid="{testid}"] [dir="ltr"]'
            el = page.query_selector(selector)
            if el:
                text = el.text_content() or ""
                num = self._parse_metric_text(text)
                if num > 0:
                    return num
        except Exception:
            pass

        # 方法2: [data-testid] の親要素テキスト
        try:
            selector = f'[data-testid="{testid}"]'
            el = page.query_selector(selector)
            if el:
                # 親要素を取得してテキストを抽出
                parent = el.evaluate_handle("el => el.parentElement")
                if parent:
                    text = parent.evaluate("el => el.textContent") or ""
                    num = self._parse_metric_text(text)
                    if num > 0:
                        return num
        except Exception:
            pass

        # 方法3: aria-label からの数値抽出
        try:
            selector = f'[data-testid="{testid}"]'
            el = page.query_selector(selector)
            if el:
                label = el.get_attribute("aria-label") or ""
                num = self._extract_number_from_label(label)
                if num > 0:
                    return num
        except Exception:
            pass

        return 0

    @staticmethod
    def _parse_metric_text(text: str) -> int:
        """メトリクステキストを整数に変換。

        "1,234" -> 1234, "1.2K" -> 1200, "5.3M" -> 5300000,
        "12万" -> 120000, "" -> 0

        Args:
            text: メトリクスのテキスト表現

        Returns:
            変換後の整数値
        """
        if not text:
            return 0

        text = text.strip()
        if not text:
            return 0

        # 「万」対応 (例: "1.2万" -> 12000, "12万" -> 120000)
        man_match = re.match(r'^([\d,.]+)\s*万$', text)
        if man_match:
            num_str = man_match.group(1).replace(",", "")
            try:
                return int(float(num_str) * 10000)
            except ValueError:
                return 0

        # K/M/B 表記対応
        suffix_match = re.match(r'^([\d,.]+)\s*([KkMmBb])$', text)
        if suffix_match:
            num_str = suffix_match.group(1).replace(",", "")
            suffix = suffix_match.group(2).upper()
            multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
            try:
                return int(float(num_str) * multipliers[suffix])
            except (ValueError, KeyError):
                return 0

        # カンマ区切り数値 (例: "1,234" -> 1234)
        clean = text.replace(",", "")
        # 数値部分のみ抽出
        num_match = re.match(r'^[\d.]+$', clean)
        if num_match:
            try:
                return int(float(clean))
            except ValueError:
                return 0

        # テキスト中から最初の数値を抽出
        digits = re.search(r'[\d,]+\.?\d*', text)
        if digits:
            clean = digits.group().replace(",", "")
            try:
                return int(float(clean))
            except ValueError:
                pass

        return 0

    @staticmethod
    def _extract_number_from_label(label: str) -> int:
        """aria-label等から数値を抽出。

        例: "1,234 views" -> 1234, "5.3K 表示" -> 5300

        Args:
            label: aria-label等のテキスト

        Returns:
            抽出された数値
        """
        if not label:
            return 0

        # 数値部分とサフィックスを抽出
        match = re.search(r'([\d,]+\.?\d*)\s*([KkMmBb万])?', label)
        if match:
            text = match.group(0).strip()
            return ImpressionScraper._parse_metric_text(text)

        return 0

    def _human_wait(
        self, min_sec: float = 1.0, max_sec: float = 3.0
    ) -> None:
        """ランダム待機。

        Args:
            min_sec: 最小待機秒数
            max_sec: 最大待機秒数
        """
        time.sleep(random.uniform(min_sec, max_sec))

    @staticmethod
    def _take_error_screenshot(page) -> None:
        """エラー時のスクリーンショットを保存。

        Args:
            page: Playwrightのページオブジェクト
        """
        try:
            timestamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
            screenshot_dir = Path("output/posting")
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            path = screenshot_dir / f"impression_error_{timestamp}.png"
            page.screenshot(path=str(path))
            logger.info("エラースクリーンショット保存: %s", path)
        except Exception as exc:
            logger.warning("スクリーンショット保存失敗: %s", exc)
