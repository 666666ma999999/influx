"""Inactive checker extension.

Wraps the existing collector.inactive_checker module as an extension
so it can participate in the tier1 collection pipeline via the EventBus.

IMPORTANT: collector.inactive_checker uses Playwright, which is heavy.
All imports from that module are lazy (inside on_validate) to avoid
importing Playwright at extension registration time.
"""

import logging
from typing import Any, Dict, List, Set

from core.registry import Extension

logger = logging.getLogger(__name__)


class InactiveCheckerExtension(Extension):
    """Extension wrapper around the inactive account checker.

    On setup, subscribes to the tier1.influencer.validate hook.
    On the hook event, lazy-imports the collector.inactive_checker module
    and delegates to run_inactive_check / check_account_status /
    detect_inactive_accounts depending on the payload shape.
    """

    def __init__(self) -> None:
        self._event_bus = None
        self._config: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "tier1.inactive_checker"

    def setup(self, context: Any) -> None:
        """Subscribe to the influencer.validate hook.

        Args:
            context: Dict with event_bus, config, and registry.
        """
        self._event_bus = (
            context.get("event_bus")
            if isinstance(context, dict)
            else getattr(context, "event_bus", None)
        )
        self._config = (
            context.get("config", {})
            if isinstance(context, dict)
            else getattr(context, "config", {})
        ) or {}

        if self._event_bus is not None:
            self._event_bus.subscribe(
                "tier1.influencer.validate", self.on_validate, priority=100
            )

        logger.info("InactiveCheckerExtension setup complete")

    def on_validate(
        self,
        event: str,
        payload: Dict[str, Any],
        meta: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Validate influencer account(s) for activity status.

        Supports two payload shapes:
          - Single account: {"username": "some_user", ...}
          - Batch accounts: {"usernames": ["user1", "user2"], ...}

        Optional payload keys:
          - profile_path (str): Browser profile path. Default "./x_profile".
          - threshold_days (int): Days before inactive. Default 30.

        Args:
            event: Event name (tier1.influencer.validate).
            payload: Must contain "username" or "usernames".
            meta: Event metadata (correlation_id etc.).

        Returns:
            Dict with "results" (list) and "inactive_usernames" (set).
        """
        # Lazy-import to avoid pulling in Playwright at registration time
        from collector.inactive_checker import (
            check_account_status,
            detect_inactive_accounts,
            run_inactive_check,
        )

        profile_path: str = payload.get(
            "profile_path",
            self._config.get("profile_path", "./x_profile"),
        )
        threshold_days: int = payload.get(
            "threshold_days",
            self._config.get("threshold_days", 30),
        )
        use_cache: bool = payload.get(
            "use_cache",
            self._config.get("use_cache", True),
        )

        username: str = payload.get("username", "")
        usernames: List[str] = payload.get("usernames", [])

        results: List[Dict[str, Any]] = []
        inactive_usernames: Set[str] = set()

        if username and not usernames:
            # Single account check via run_inactive_check (handles caching)
            all_results = run_inactive_check(
                profile_path=profile_path,
                use_cache=use_cache,
            )
            # Filter to the requested username
            results = [r for r in all_results if r["username"] == username]
            inactive_usernames = detect_inactive_accounts(
                results, threshold_days=threshold_days
            )
        elif usernames:
            # Batch check
            all_results = run_inactive_check(
                profile_path=profile_path,
                use_cache=use_cache,
            )
            # Filter to the requested usernames
            requested = set(usernames)
            results = [r for r in all_results if r["username"] in requested]
            inactive_usernames = detect_inactive_accounts(
                results, threshold_days=threshold_days
            )
        else:
            logger.warning(
                "on_validate called without 'username' or 'usernames' in payload"
            )

        output = {
            "results": results,
            "inactive_usernames": inactive_usernames,
        }

        if self._event_bus is not None:
            self._event_bus.publish(
                "influencer.validation.completed",
                {
                    "results": results,
                    "inactive_usernames": list(inactive_usernames),
                    "threshold_days": threshold_days,
                },
                meta,
            )

        return output

    def teardown(self) -> None:
        """Clean up resources."""
        if self._event_bus is not None:
            try:
                self._event_bus.unsubscribe(
                    "tier1.influencer.validate", self.on_validate
                )
            except ValueError:
                pass
        self._event_bus = None
        self._config = {}
        logger.info("InactiveCheckerExtension teardown complete")
