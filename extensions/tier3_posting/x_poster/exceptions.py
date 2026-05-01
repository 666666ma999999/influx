"""tier3_posting/x_poster の共有例外（shared/ への薄い re-export）。

Phase 1 (2026-05-01): SST を tier3_posting/shared/exceptions.py に移し、
ここは backward compat のための再エクスポート。
"""

from ..shared.exceptions import CookieExpiredError  # noqa: F401

__all__ = ["CookieExpiredError"]
