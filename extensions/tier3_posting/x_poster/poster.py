"""Playwright経由でXに投稿するクラス。

SafeXCollectorのCookie認証・人間らしい操作パターンを踏襲。
"""

import logging
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from dateutil.parser import parse as parse_datetime

logger = logging.getLogger(__name__)


class XPoster:
    """Playwright経由でXに投稿する。

    Attributes:
        profile_path: ログイン済みブラウザプロファイルのパス
    """

    def __init__(self, profile_path: str = "./x_profile") -> None:
        self.profile_path = Path(profile_path).resolve()

    def post(
        self, body: str, images: list[str] | None = None,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """Xに投稿する。

        Args:
            body: 投稿本文
            images: 添付画像パスリスト（最大4枚）
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

            # 画像添付
            if images:
                self._attach_images(page, images)

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

    def schedule_post(
        self, body: str, scheduled_at: str,
        images: list[str] | None = None,
        dry_run: bool = True,
    ) -> dict:
        """X予約投稿（Playwright経由でネイティブUIを操作）

        Args:
            body: 投稿本文
            scheduled_at: 予約日時 (ISO 8601 JST, e.g. "2026-03-15T09:00:00+09:00")
            images: 添付画像パスリスト（最大4枚）
            dry_run: Trueの場合、投稿直前で停止

        Returns:
            dict: {"success": bool, "scheduled_at": str, "error": str|None}
        """
        dt = parse_datetime(scheduled_at)
        if dt.tzinfo is None:
            # タイムゾーンなしの場合はJSTとして扱う
            from dateutil.tz import gettz
            dt = dt.replace(tzinfo=gettz("Asia/Tokyo"))

        from playwright.sync_api import sync_playwright

        cookies = self._load_cookies()
        if not cookies:
            return {
                "success": False,
                "scheduled_at": scheduled_at,
                "error": "Cookie読込失敗: cookies.jsonが見つからないか空です",
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
                    "scheduled_at": scheduled_at,
                    "error": "ログインしていません。setup_profile.pyを実行してください",
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
                    "scheduled_at": scheduled_at,
                    "error": "投稿テキストエリアが見つかりません",
                }

            # クリックしてフォーカス
            textarea.click()
            self._human_wait(0.3, 0.8)

            # 人間らしいタイピング
            self._human_type(page, body)
            self._human_wait(1.0, 2.0)

            # 画像添付
            if images:
                self._attach_images(page, images)

            # スケジュールアイコンをクリック
            schedule_icon = None
            for selector in [
                '[data-testid="scheduleOption"]',
                '[aria-label="Schedule"]',
                '[aria-label="スケジュール"]',
            ]:
                try:
                    schedule_icon = page.wait_for_selector(selector, timeout=3000)
                    if schedule_icon:
                        break
                except Exception:
                    continue

            if not schedule_icon:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                page.screenshot(
                    path=f"output/posting/schedule_error_{timestamp}.png"
                )
                return {
                    "success": False,
                    "scheduled_at": scheduled_at,
                    "error": "スケジュールアイコンが見つかりません",
                }

            schedule_icon.click()
            self._human_wait(1.0, 2.0)

            # 日時ピッカーで予約日時を設定
            self._set_schedule_datetime(page, dt)
            self._human_wait(0.5, 1.0)

            if dry_run:
                # スクリーンショットを撮って終了
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_path = f"output/posting/schedule_dryrun_{timestamp}.png"
                page.screenshot(path=screenshot_path)
                logger.info(
                    "🔖 [DRY RUN] 予約投稿を確認: %s → %s",
                    scheduled_at, screenshot_path,
                )
                return {
                    "success": True,
                    "scheduled_at": scheduled_at,
                    "error": None,
                }

            # 予約確定ボタンをクリック
            confirm_button = None
            for selector in [
                '[data-testid="scheduledConfirmationPrimaryAction"]',
                'button[data-testid="scheduledConfirmationPrimaryAction"]',
            ]:
                try:
                    confirm_button = page.wait_for_selector(selector, timeout=5000)
                    if confirm_button:
                        break
                except Exception:
                    continue

            if not confirm_button:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                page.screenshot(
                    path=f"output/posting/schedule_error_{timestamp}.png"
                )
                return {
                    "success": False,
                    "scheduled_at": scheduled_at,
                    "error": "予約確定ボタンが見つかりません",
                }

            confirm_button.click()
            self._human_wait(3.0, 5.0)

            logger.info("📅 予約投稿完了: %s", scheduled_at)
            return {
                "success": True,
                "scheduled_at": scheduled_at,
                "error": None,
            }

        except Exception as exc:
            logger.exception("予約投稿中にエラーが発生")
            try:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                page.screenshot(
                    path=f"output/posting/schedule_error_{timestamp}.png"
                )
            except Exception:
                pass
            return {
                "success": False,
                "scheduled_at": scheduled_at,
                "error": str(exc),
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

    def _set_schedule_datetime(self, page, dt: datetime) -> None:
        """日時ピッカーを操作して予約日時を設定。

        Args:
            page: Playwrightのページオブジェクト
            dt: 設定する日時（timezone-aware datetime）
        """
        # --- 日付フィールド ---
        # Month (1-12)
        month_value = str(dt.month)
        for selector in [
            'select[data-testid="scheduledDatePickerMonths"]',
            'select[name="month"]',
        ]:
            try:
                el = page.wait_for_selector(selector, timeout=3000)
                if el:
                    el.select_option(value=month_value)
                    self._human_wait(0.2, 0.5)
                    break
            except Exception:
                continue
        else:
            # フォールバック: JavaScript injection
            page.evaluate(
                f"""() => {{
                    const sel = document.querySelector(
                        'select[data-testid="scheduledDatePickerMonths"]'
                    );
                    if (sel) {{
                        sel.value = '{month_value}';
                        sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                    }}
                }}"""
            )

        # Day (1-31)
        day_value = str(dt.day)
        for selector in [
            'select[data-testid="scheduledDatePickerDays"]',
            'select[name="day"]',
        ]:
            try:
                el = page.wait_for_selector(selector, timeout=3000)
                if el:
                    el.select_option(value=day_value)
                    self._human_wait(0.2, 0.5)
                    break
            except Exception:
                continue
        else:
            page.evaluate(
                f"""() => {{
                    const sel = document.querySelector(
                        'select[data-testid="scheduledDatePickerDays"]'
                    );
                    if (sel) {{
                        sel.value = '{day_value}';
                        sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                    }}
                }}"""
            )

        # Year
        year_value = str(dt.year)
        for selector in [
            'select[data-testid="scheduledDatePickerYears"]',
            'select[name="year"]',
        ]:
            try:
                el = page.wait_for_selector(selector, timeout=3000)
                if el:
                    el.select_option(value=year_value)
                    self._human_wait(0.2, 0.5)
                    break
            except Exception:
                continue
        else:
            page.evaluate(
                f"""() => {{
                    const sel = document.querySelector(
                        'select[data-testid="scheduledDatePickerYears"]'
                    );
                    if (sel) {{
                        sel.value = '{year_value}';
                        sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                    }}
                }}"""
            )

        # --- 時刻フィールド ---
        # Hour (12-hour format)
        hour_12 = dt.hour % 12
        if hour_12 == 0:
            hour_12 = 12
        hour_value = str(hour_12)

        for selector in [
            'select[data-testid="scheduledDatePickerHours"]',
            'input[data-testid="scheduledDatePickerHours"]',
            'select[name="hours"]',
        ]:
            try:
                el = page.wait_for_selector(selector, timeout=3000)
                if el:
                    tag = el.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        el.select_option(value=hour_value)
                    else:
                        el.fill(hour_value)
                    self._human_wait(0.2, 0.5)
                    break
            except Exception:
                continue

        # Minute
        minute_value = str(dt.minute).zfill(2)
        for selector in [
            'select[data-testid="scheduledDatePickerMinutes"]',
            'input[data-testid="scheduledDatePickerMinutes"]',
            'select[name="minutes"]',
        ]:
            try:
                el = page.wait_for_selector(selector, timeout=3000)
                if el:
                    tag = el.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        el.select_option(value=minute_value)
                    else:
                        el.fill(minute_value)
                    self._human_wait(0.2, 0.5)
                    break
            except Exception:
                continue

        # AM/PM
        ampm_value = "AM" if dt.hour < 12 else "PM"
        for selector in [
            'select[data-testid="scheduledDatePickerMeridiem"]',
            'select[name="amPm"]',
        ]:
            try:
                el = page.wait_for_selector(selector, timeout=3000)
                if el:
                    el.select_option(value=ampm_value)
                    self._human_wait(0.2, 0.5)
                    break
            except Exception:
                continue
        else:
            page.evaluate(
                f"""() => {{
                    const sel = document.querySelector(
                        'select[data-testid="scheduledDatePickerMeridiem"]'
                    );
                    if (sel) {{
                        sel.value = '{ampm_value}';
                        sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                    }}
                }}"""
            )

    def _attach_images(self, page, images: list[str]) -> None:
        """画像ファイルを添付（最大4枚）。

        Args:
            page: Playwrightのページオブジェクト
            images: 添付する画像ファイルのパスリスト
        """
        images_to_attach = images[:4]  # 最大4枚に制限
        if len(images) > 4:
            logger.warning(
                "画像は最大4枚まで。%d枚中4枚のみ添付します", len(images)
            )

        for i, image_path in enumerate(images_to_attach):
            image_path_resolved = str(Path(image_path).resolve())

            # ファイル入力要素を探す
            file_input = None
            for selector in [
                'input[data-testid="fileInput"]',
                'input[type="file"][accept*="image"]',
                'input[type="file"]',
            ]:
                try:
                    file_input = page.wait_for_selector(
                        selector, timeout=5000, state="attached"
                    )
                    if file_input:
                        break
                except Exception:
                    continue

            if not file_input:
                logger.warning("画像ファイル入力が見つかりません（%d枚目）", i + 1)
                break

            file_input.set_input_files(image_path_resolved)
            self._human_wait(1.5, 3.0)

            # サムネイルプレビューの表示を待機
            for preview_selector in [
                '[data-testid="attachments"]',
                '[data-testid="mediaBadge"]',
                'div[aria-label="Media"]',
            ]:
                try:
                    page.wait_for_selector(preview_selector, timeout=5000)
                    break
                except Exception:
                    continue

            logger.info("📎 画像添付完了 (%d/%d): %s", i + 1, len(images_to_attach), image_path)

            # 複数枚の場合、人間らしい間隔を空ける
            if i < len(images_to_attach) - 1:
                self._human_wait(0.5, 1.5)

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
