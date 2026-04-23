"""tier3_posting/x_poster の共有例外クラス。

plan.md M1 T1.5 で `XPoster` と `ImpressionScraper` の Cookie 失効通知を統一するため、
`CookieExpiredError` を Single Source of Truth としてここに集約する。
"""


class CookieExpiredError(Exception):
    """Cookie が X.com 認証セッションで無効になった場合に raise する。

    XPoster.post() / XPoster.schedule_post() / ImpressionScraper.scrape() の
    各エントリポイントで Cookie 読込失敗・ログイン画面リダイレクト検出時に送出される。
    """
