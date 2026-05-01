"""CookieExpiredError の SST 契約テスト（plan.md M0 Stage2 M5）。

- factory method (`missing` / `empty` / `login_redirect` / `expired`) が
  正しい `reason` 属性を持つインスタンスを返す
- デフォルトコンストラクタは `reason="unknown"` になる
- tier3_posting 側の再エクスポートが同一クラスを指す（SST）

実行:
  docker compose run --rm xstock python -m unittest tests.test_collector_exceptions -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector.exceptions import (
    CookieExpiredError,
    REASON_EMPTY,
    REASON_EXPIRED,
    REASON_LOGIN_REDIRECT,
    REASON_MISSING,
    REASON_UNKNOWN,
)


class TestReasonConstants(unittest.TestCase):
    def test_constants_are_stable_strings(self):
        # 呼び出し側が if e.reason == REASON_LOGIN_REDIRECT で分岐するため文字列値を固定
        self.assertEqual(REASON_MISSING, "missing")
        self.assertEqual(REASON_EMPTY, "empty")
        self.assertEqual(REASON_LOGIN_REDIRECT, "login_redirect")
        self.assertEqual(REASON_EXPIRED, "expired")
        self.assertEqual(REASON_UNKNOWN, "unknown")


class TestFactoryMethods(unittest.TestCase):
    def test_missing_factory(self):
        exc = CookieExpiredError.missing(Path("/tmp/x_profile/cookies.json"))
        self.assertIsInstance(exc, CookieExpiredError)
        self.assertEqual(exc.reason, REASON_MISSING)
        self.assertIn("cookies.json", str(exc))

    def test_empty_factory(self):
        exc = CookieExpiredError.empty(Path("/tmp/x_profile/cookies.json"))
        self.assertIsInstance(exc, CookieExpiredError)
        self.assertEqual(exc.reason, REASON_EMPTY)
        self.assertIn("空", str(exc))

    def test_login_redirect_factory(self):
        exc = CookieExpiredError.login_redirect("https://x.com/i/flow/login")
        self.assertEqual(exc.reason, REASON_LOGIN_REDIRECT)
        self.assertIn("x.com/i/flow/login", str(exc))

    def test_login_redirect_with_detail(self):
        exc = CookieExpiredError.login_redirect(
            "https://x.com/home", detail="batch login_required x3"
        )
        self.assertEqual(exc.reason, REASON_LOGIN_REDIRECT)
        self.assertIn("batch login_required x3", str(exc))

    def test_expired_factory(self):
        exc = CookieExpiredError.expired()
        self.assertEqual(exc.reason, REASON_EXPIRED)
        self.assertIn("期限切れ", str(exc))

    def test_expired_with_detail(self):
        exc = CookieExpiredError.expired(detail="auth_token missing")
        self.assertEqual(exc.reason, REASON_EXPIRED)
        self.assertIn("auth_token missing", str(exc))


class TestDefaultConstructor(unittest.TestCase):
    def test_default_reason_is_unknown(self):
        # 後方互換: reason 未指定なら "unknown"
        exc = CookieExpiredError("raw message")
        self.assertEqual(exc.reason, REASON_UNKNOWN)
        self.assertEqual(str(exc), "raw message")

    def test_reason_kwarg_override(self):
        exc = CookieExpiredError("custom", reason=REASON_EMPTY)
        self.assertEqual(exc.reason, REASON_EMPTY)


# 2026-05-01 Phase 3: tier3_posting を別リポに物理分離。compat テストは
# tier3_posting リポに移管した（influx 側からは tier3_posting を import 不能）。

class TestBranchingByReason(unittest.TestCase):
    """呼び出し側が except CookieExpiredError as e: if e.reason == ... で分岐できることを確認。"""

    def test_caller_can_route_by_reason(self):
        reasons_seen = []
        for exc in [
            CookieExpiredError.missing(Path("/a")),
            CookieExpiredError.empty(Path("/b")),
            CookieExpiredError.login_redirect("https://x.com/login"),
            CookieExpiredError.expired(),
        ]:
            try:
                raise exc
            except CookieExpiredError as e:
                reasons_seen.append(e.reason)

        self.assertEqual(
            reasons_seen,
            [REASON_MISSING, REASON_EMPTY, REASON_LOGIN_REDIRECT, REASON_EXPIRED],
        )


if __name__ == "__main__":
    unittest.main()
