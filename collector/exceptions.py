"""collector パッケージ共通の例外クラス（Single Source of Truth）。

plan.md M0 T0.6: `CookieExpiredError` を収集系で共通 SST 化。
2026-05-01 Phase 3: tier3_posting は別リポ autopost に物理分離。同 reason 体系の独立 fork が
~/Desktop/biz/autopost/tier3_posting/shared/exceptions.py に存在。
"""
from __future__ import annotations


# plan.md M0 Stage2 M5: 失効理由の識別子。呼び出し側が e.reason で分岐できるよう
# 揃えた軽量タクソノミー。未指定時は "unknown"（後方互換）。
REASON_MISSING = "missing"           # cookies.json 自体が存在しない
REASON_EMPTY = "empty"               # 存在するが Cookie が空（復号失敗含む）
REASON_LOGIN_REDIRECT = "login_redirect"  # X のログイン画面へ遷移した
REASON_EXPIRED = "expired"           # 期限切れ Cookie を検出
REASON_UNKNOWN = "unknown"           # 後方互換（理由特定不能）


class CookieExpiredError(Exception):
    """Cookie が X.com 認証セッションで無効になった場合に raise する。

    収集系: `SafeXCollector.collect()` / `inactive_checker.run_inactive_check()` の
    Cookie 読込失敗・ログイン画面リダイレクト検出時に送出される。

    投稿系: `XPoster.post()` / `XPoster.schedule_post()` / `ImpressionScraper.scrape_batch()` の
    各エントリポイントで同様に送出される。

    reason は `missing` / `empty` / `login_redirect` / `expired` / `unknown` の
    いずれか（上記 REASON_* 定数参照）。呼び出し側は `except CookieExpiredError as e`
    でまとめて捕捉し、リトライ戦略（再ログイン vs 単純再実行）を e.reason で判定する。
    """

    def __init__(self, message: str, *, reason: str = REASON_UNKNOWN) -> None:
        super().__init__(message)
        self.reason = reason

    @classmethod
    def missing(cls, cookie_path) -> "CookieExpiredError":
        return cls(
            f"Cookie ファイルが見つかりません: {cookie_path} "
            "（scripts/import_chrome_cookies.py で Chrome から再取得してください）",
            reason=REASON_MISSING,
        )

    @classmethod
    def empty(cls, cookie_path) -> "CookieExpiredError":
        return cls(
            f"Cookie が空です: {cookie_path} "
            "（scripts/import_chrome_cookies.py で再取得してください）",
            reason=REASON_EMPTY,
        )

    @classmethod
    def login_redirect(cls, current_url: str, detail: str = "") -> "CookieExpiredError":
        suffix = f" [{detail}]" if detail else ""
        return cls(
            f"ログイン画面にリダイレクト（Cookie 失効の可能性）: {current_url}{suffix}",
            reason=REASON_LOGIN_REDIRECT,
        )

    @classmethod
    def expired(cls, detail: str = "") -> "CookieExpiredError":
        base = (
            "Cookie が期限切れです。scripts/import_chrome_cookies.py で Chrome から"
            "再抽出してください（refresh-x-cookies スキル参照）。"
        )
        if detail:
            base = f"{base} [{detail}]"
        return cls(base, reason=REASON_EXPIRED)
