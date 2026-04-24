"""tier3_posting/x_poster の共有例外（collector SST の再エクスポート）。

plan.md M0 T0.6: `CookieExpiredError` の SST を `collector.exceptions` に移設し、
tier3_posting は backward compat のため同名で再エクスポートする。
"""

from collector.exceptions import CookieExpiredError  # noqa: F401

__all__ = ["CookieExpiredError"]
