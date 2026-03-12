"""Playwright経由でXに投稿するクラス。

SafeXCollectorのCookie認証・人間らしい操作パターンを踏襲。
"""

import logging
import random
import time
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


class XPoster:
    """Playwright経由でXに投稿する。

    Attributes:
        profile_path: ログイン済みブラウザプロファイルのパス
    """

    def __init__(self, profile_path: str = "./x_profile") -> None:
        self.profile_path = Path(profile_path).resolve()

    def post(self, body: str, dry_run: bool = True) -> Dict[str, Any]:
        """Xに投稿する。

        Args:
            body: 投稿本文
            dry_run: Trueの場合は実投稿しない

        Returns:
            {"success": bool, "posted_url": str, "error": str, "dry_run": bool}
        """
        if dry_run:
            logger.info("[DRY RUN] Would post: %s...", body[:80])
            return {"success": True, "posted_url": "", "error": "", "dry_run": True}

        from playwright.sync_api import sync_playwright

        cookies = self._load_cookies()
        if not cookies:
            return {
                "success": False,
                "posted_url": "",
                "error": "Cookie読込失敗: cookies.jsonが見つからないか空です",
                "dry_run": False,
            }

        browser = None
        context = None
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

            # Xのホームに遷移してログイン確認
            page.goto("https://twitter.com/home", wait_until="domcontentloaded")
            self._human_wait(2.0, 4.0)

            if not self._check_login_status(page):
                return {
                    "success": False,
                    "posted_url": "",
                    "error": "ログインしていません。setup_profile.pyを実行してください",
                    "dry_run": False,
                }

            # 投稿画面に遷移
            page.goto(
                "https://twitter.com/compose/tweet",
                wait_until="domcontentloaded",
            )
            self._human_wait(1.5, 3.0)

            # テキストエリアを待機して入力
            textarea = page.wait_for_selector(
                '[data-testid="tweetTextarea_0"]', timeout=15000
            )
            if not textarea:
                return {
                    "success": False,
                    "posted_url": "",
                    "error": "投稿テキストエリアが見つかりません",
                    "dry_run": False,
                }

            # クリックしてフォーカス
            textarea.click()
            self._human_wait(0.3, 0.8)

            # 人間らしいタイピング
            self._human_type(page, body)
            self._human_wait(1.0, 2.0)

            # 投稿ボタンクリック
            post_button = page.wait_for_selector(
                '[data-testid="tweetButton"]', timeout=10000
            )
            if not post_button:
                return {
                    "success": False,
                    "posted_url": "",
                    "error": "投稿ボタンが見つかりません",
                    "dry_run": False,
                }

            post_button.click()
            self._human_wait(3.0, 5.0)

            # 投稿完了確認（ツイートURLの取得を試みる）
            posted_url = self._capture_posted_url(page)

            logger.info("投稿成功: %s", posted_url or "(URL取得不可)")
            return {
                "success": True,
                "posted_url": posted_url,
                "error": "",
                "dry_run": False,
            }

        except Exception as exc:
            logger.exception("投稿中にエラーが発生")
            return {
                "success": False,
                "posted_url": "",
                "error": str(exc),
                "dry_run": False,
            }
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

    def _human_type(self, page, text: str) -> None:
        """人間らしいタイピング（10-50ms間隔）。

        Args:
            page: Playwrightのページオブジェクト
            text: 入力するテキスト
        """
        for char in text:
            page.keyboard.type(char)
            time.sleep(random.uniform(0.01, 0.05))

    def _human_wait(self, min_sec: float = 1.0, max_sec: float = 3.0) -> None:
        """ランダム待機。

        Args:
            min_sec: 最小待機秒数
            max_sec: 最大待機秒数
        """
        time.sleep(random.uniform(min_sec, max_sec))

    def _check_login_status(self, page) -> bool:
        """ログイン状態を確認。

        Args:
            page: Playwrightのページオブジェクト

        Returns:
            True: ログイン済み, False: 未ログイン
        """
        try:
            page.wait_for_selector(
                '[data-testid="SideNav_AccountSwitcher_Button"], '
                '[data-testid="AppTabBar_Home_Link"]',
                timeout=10000,
            )
            return True
        except Exception:
            return False

    def _capture_posted_url(self, page) -> str:
        """投稿後のツイートURLを取得する。

        Args:
            page: Playwrightのページオブジェクト

        Returns:
            ツイートURL（取得できない場合は空文字列）
        """
        try:
            # 投稿成功のトースト通知から直前の投稿へのリンクを探す
            # もしくはホームタイムラインに遷移して最新投稿のURLを取得
            page.goto("https://twitter.com/home", wait_until="domcontentloaded")
            self._human_wait(2.0, 4.0)

            # 最新のツイートリンクを取得
            links = page.query_selector_all('a[href*="/status/"]')
            for link in links:
                href = link.get_attribute("href")
                if href and "/status/" in href:
                    if href.startswith("/"):
                        href = f"https://twitter.com{href}"
                    return href
        except Exception as exc:
            logger.warning("投稿URL取得失敗: %s", exc)

        return ""
