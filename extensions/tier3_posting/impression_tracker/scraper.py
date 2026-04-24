"""投稿済みツイートのインプレッション追跡スクレイパー。

SafeXCollector / XPosterのCookie認証・人間らしい操作パターンを踏襲。
"""

import logging
import random
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

from collector.exceptions import CookieExpiredError  # noqa: F401, E402


class ImpressionScraper:
    """投稿済みツイートのインプレッション（Views/Likes/RT/Reply/Bookmark）をスクレイピング。

    Attributes:
        profile_path: ログイン済みブラウザプロファイルのパス
    """

    def __init__(
        self,
        profile_path: str = "./x_profile",
        screenshot_dir: Optional[str] = None,
    ) -> None:
        self.profile_path = Path(profile_path).resolve()
        self._screenshot_dir = screenshot_dir

    def scrape_batch(
        self,
        urls: List[str],
        screenshot_dir: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """複数ツイートURLのエンゲージメント指標を1ブラウザセッションで取得。

        Args:
            urls: ツイートURLのリスト
            screenshot_dir: エラー時スクリーンショット保存先（Noneなら self._screenshot_dir を使用）

        Returns:
            List[dict]: 各URLの結果 (T0スキーマ)
                成功: {"url", "status": "ok", "likes", "views", "retweets", "replies", "bookmarks", "scraped_at"}
                失敗: {"url", "status": <分類>, "error_detail", "scraped_at"}

        Raises:
            CookieExpiredError: バッチ先頭のCookie pre-flightで /login へのリダイレクトを検出した場合
        """
        from playwright.sync_api import sync_playwright

        # Save/restore to avoid persistent instance state mutation.
        original_screenshot_dir = self._screenshot_dir
        if screenshot_dir:
            self._screenshot_dir = screenshot_dir

        try:
            cookies = self._load_cookies()
        except CookieExpiredError:
            self._screenshot_dir = original_screenshot_dir
            raise

        pw = None
        browser = None
        context = None
        results: List[Dict[str, Any]] = []

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

            # Cookie pre-flight: ログインセッションが有効か確認
            page.goto("https://x.com/home", wait_until="domcontentloaded")
            self._human_wait(2.0, 4.0)
            if "/i/flow/login" in page.url or "/login" in page.url:
                raise CookieExpiredError.login_redirect(page.url, detail="pre-flight")

            consecutive_login_required = 0

            for url in urls:
                self._human_wait(2.0, 4.0)
                result = self._run_on_page(page, url)

                # Rate limit: 30秒待機して1回だけretry。再発したら停止。
                if result.get("status") == "rate_limited":
                    logger.warning("rate_limited 検出、30秒待機してretry: %s", url)
                    time.sleep(30)
                    retry_result = self._run_on_page(page, url)
                    results.append(retry_result)
                    if retry_result.get("status") == "rate_limited":
                        logger.error("retry後もrate_limited、バッチ停止")
                        break
                    consecutive_login_required = 0
                    continue

                results.append(result)

                # login_required が連続3回でバッチ停止（Cookie失効とみなす）
                if result.get("status") == "login_required":
                    consecutive_login_required += 1
                    if consecutive_login_required >= 3:
                        raise CookieExpiredError.login_redirect(
                            page.url, detail="batch login_required x3"
                        )
                else:
                    consecutive_login_required = 0

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
            self._screenshot_dir = original_screenshot_dir

        return results

    def _run_on_page(self, page, url: str) -> Dict[str, Any]:
        """1URLのエンゲージメント取得（scrape_batch内のURL毎処理）。

        Args:
            page: Playwrightのページオブジェクト（セッション共有）
            url: ツイートURL

        Returns:
            T0スキーマの dict
        """
        now = datetime.now(JST).isoformat()
        try:
            page.goto(url, wait_until="domcontentloaded")

            try:
                page.wait_for_selector('[data-testid="tweetText"]', timeout=15000)
            except Exception as wait_exc:
                status, error_detail = self._classify_error(page, wait_exc)
                self._take_error_screenshot(page)
                logger.warning("ツイート本文が見つかりません (%s): %s", status, url)
                return {"url": url, "status": status, "error_detail": error_detail, "scraped_at": now}

            self._human_wait(1.0, 2.0)

            views = self._scrape_impressions(page)
            likes = self._scrape_metric(page, "like")
            retweets = self._scrape_metric(page, "retweet")
            replies = self._scrape_metric(page, "reply")
            bookmarks = self._scrape_metric(page, "bookmark")

            logger.info(
                "取得成功: %s (views=%d, likes=%d, RT=%d)",
                url, views, likes, retweets,
            )
            return {
                "url": url,
                "status": "ok",
                "likes": likes,
                "views": views,
                "retweets": retweets,
                "replies": replies,
                "bookmarks": bookmarks,
                "scraped_at": now,
            }

        except Exception as exc:
            logger.exception("取得エラー: %s", url)
            try:
                self._take_error_screenshot(page)
            except Exception:
                pass
            status, error_detail = self._classify_error(page, exc)
            return {"url": url, "status": status, "error_detail": error_detail, "scraped_at": now}

    def _classify_error(
        self, page, exc: Optional[Exception] = None
    ) -> Tuple[str, str]:
        """ページ状態と例外からエラーステータスを分類する。

        Args:
            page: Playwrightのページオブジェクト
            exc: 発生した例外（任意）

        Returns:
            (status, error_detail) のタプル
            status: "login_required" | "deleted" | "rate_limited" | "protected" | "other"
        """
        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""

        # 1. ログインリダイレクト
        if "/login" in current_url or "/i/flow/login" in current_url:
            return ("login_required", current_url)

        try:
            body = page.content() or ""
        except Exception:
            body = ""

        # 2. 削除済みツイート
        for phrase in (
            "Hmm\u2026this page doesn't exist",
            "このページは存在しません",
            "Page not found",
        ):
            if phrase in body:
                snippet = body[max(0, body.find(phrase) - 20):body.find(phrase) + 60]
                return ("deleted", snippet.strip())

        # 3. レート制限
        for phrase in ("Rate limit", "レート制限"):
            if phrase in body:
                return ("rate_limited", phrase)

        # 4. 鍵アカ（保護されたアカウント）
        for phrase in ("These posts are protected", "保護されています"):
            if phrase in body:
                return ("protected", phrase)
        try:
            protected_el = page.query_selector('[data-testid="empty_state_header_text"]')
            if protected_el:
                return ("protected", protected_el.text_content() or "protected")
        except Exception:
            pass

        # 5. 例外メッセージ
        if exc is not None:
            return ("other", str(exc))

        return ("other", "Unknown")

    def scrape(self, tweet_url: str) -> Dict[str, Any]:
        """ツイートのエンゲージメント指標を取得（後方互換ラッパー）。

        **T0/T1 スキーマ境界**（plan.md M1 T1.4 レビュー指摘対応）:
        - T0 (`scrape_batch` / `_run_on_page` の戻り値): 生指標 `views`/`likes`/.../`status`/`error_detail`
        - T1 (`scrape` の戻り値): UI/PostStore 向け正規化 `impressions` (views より rename) + `engagement_rate` 付与
        - T0 を直接 `PostStore.add_impression` / `review.html` に渡してはいけない（フィールド名不一致で UI が `ER -` になる）
        - T0→T1 変換は本関数内でのみ行う

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
        try:
            results = self.scrape_batch([tweet_url])
            r = results[0]
        except CookieExpiredError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            logger.exception("インプレッション取得中にエラーが発生: %s", tweet_url)
            return {"error": str(exc)}

        if r.get("status") != "ok":
            return {"error": r.get("error_detail", r.get("status", "unknown error"))}

        impressions = r.get("views", 0)
        likes = r.get("likes", 0)
        retweets = r.get("retweets", 0)
        replies = r.get("replies", 0)
        engagement_rate = (likes + retweets + replies) / max(impressions, 1)

        return {
            "tweet_url": tweet_url,
            "impressions": impressions,
            "likes": likes,
            "retweets": retweets,
            "replies": replies,
            "bookmarks": r.get("bookmarks", 0),
            "engagement_rate": round(engagement_rate, 6),
            "scraped_at": r.get("scraped_at", datetime.now(JST).isoformat()),
        }

    def _load_cookies(self) -> list:
        """x_profile/cookies.jsonからCookie読込。

        Returns:
            Playwright形式のCookieリスト

        Raises:
            CookieExpiredError: cookies.json が存在しない／空の場合。
        """
        from collector.cookie_crypto import load_cookies_or_raise
        return load_cookies_or_raise(self.profile_path / "cookies.json")

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

    def _take_error_screenshot(self, page) -> None:
        """エラー時のスクリーンショットを保存。

        Args:
            page: Playwrightのページオブジェクト
        """
        try:
            timestamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
            screenshot_dir = Path(self._screenshot_dir or "output/posting")
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            path = screenshot_dir / f"impression_error_{timestamp}.png"
            page.screenshot(path=str(path))
            logger.info("エラースクリーンショット保存: %s", path)
        except Exception as exc:
            logger.warning("スクリーンショット保存失敗: %s", exc)
