"""X (Twitter) poster extension.

Posts curated news items to X (Twitter) using Playwright with cookie-based
authentication.  The actual posting logic is intentionally stubbed out for
safety -- dry_run defaults to True so that the extension never posts to X
unless explicitly instructed.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from core.registry import Extension

logger = logging.getLogger(__name__)


class XPosterExtension(Extension):
    """Extension that posts news items to X (Twitter).

    On setup, stores config and subscribes to the tier3.post hook.
    Playwright is lazy-imported only when a real (non-dry-run) post is
    attempted, because it requires a running browser runtime that is not
    available at setup time.
    """

    def __init__(self) -> None:
        self._event_bus = None
        self._config: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "tier3.x_poster"

    def setup(self, context: Any) -> None:
        """Extract event_bus and config from context.

        Playwright is NOT imported here because it requires a running
        browser runtime. It will be lazy-imported in _post_to_x when
        dry_run is False.

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
                "tier3.post", self.on_post, priority=100
            )

        logger.info("XPosterExtension setup complete")

    def on_post(
        self, event: str, payload: Dict[str, Any], meta: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Post a news item to X (Twitter).

        Args:
            event: Event name (tier3.post).
            payload: Must contain:
                - news_item (dict, required): Dict conforming to
                  news_item.schema.json. Must have status="scheduled".
                - dry_run (bool, optional): If True (default), simulate the
                  post without actually publishing to X.
            meta: Event metadata (correlation_id etc.).

        Returns:
            Updated news_item dict with status and posted_at timestamp.
        """
        news_item = payload.get("news_item")
        if not news_item:
            error_result = {
                "status": "error",
                "error_message": "news_item is required in payload",
            }
            if self._event_bus is not None:
                self._event_bus.publish("post.failed", error_result, meta)
            return error_result

        # Validate that the news item is in "scheduled" status
        item_status = news_item.get("status")
        if item_status != "scheduled":
            error_result = {
                "status": "error",
                "news_item": news_item,
                "error_message": (
                    f"news_item must have status='scheduled', "
                    f"got '{item_status}'"
                ),
            }
            if self._event_bus is not None:
                self._event_bus.publish("post.failed", error_result, meta)
            return error_result

        dry_run = payload.get(
            "dry_run", self._config.get("dry_run", True)
        )
        body = news_item.get("body", "")
        news_id = news_item.get("news_id", "unknown")

        if dry_run:
            logger.info(
                "DRY RUN: Would post news_id=%s to X: %s",
                news_id,
                body[:100],
            )
            updated_item = dict(news_item)
            updated_item["status"] = "draft"
            result = {
                "status": "dry_run",
                "news_item": updated_item,
                "dry_run": True,
            }
            if self._event_bus is not None:
                self._event_bus.publish("post.completed", result, meta)
            return result

        # Real posting
        profile_path = payload.get(
            "profile_path",
            self._config.get("profile_path", "./x_profile"),
        )
        max_retries = self._config.get("max_retries", 3)

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                post_result = self._post_to_x(body, profile_path)

                if post_result.get("success"):
                    updated_item = dict(news_item)
                    updated_item["status"] = "posted"
                    updated_item["posted_at"] = (
                        datetime.now(timezone.utc).isoformat()
                    )
                    if post_result.get("posted_url"):
                        updated_item.setdefault("metadata", {})["posted_url"] = (
                            post_result["posted_url"]
                        )
                    result = {
                        "status": "success",
                        "news_item": updated_item,
                        "attempt": attempt,
                    }
                    if self._event_bus is not None:
                        self._event_bus.publish(
                            "post.completed", result, meta
                        )
                    return result

                last_error = post_result.get("error", "Unknown error")
                logger.warning(
                    "Post attempt %d/%d failed for news_id=%s: %s",
                    attempt,
                    max_retries,
                    news_id,
                    last_error,
                )

            except Exception as exc:
                last_error = str(exc)
                logger.exception(
                    "Post attempt %d/%d raised exception for news_id=%s",
                    attempt,
                    max_retries,
                    news_id,
                )

        # All retries exhausted
        updated_item = dict(news_item)
        updated_item["status"] = "failed"
        error_result = {
            "status": "error",
            "news_item": updated_item,
            "error_message": f"All {max_retries} attempts failed: {last_error}",
        }
        if self._event_bus is not None:
            self._event_bus.publish("post.failed", error_result, meta)
        return error_result

    def _post_to_x(self, body: str, profile_path: str) -> Dict[str, Any]:
        """Post content to X (Twitter) using Playwright via XPoster.

        Args:
            body: The text content to post to X.
            profile_path: Path to browser profile with authenticated cookies.

        Returns:
            Dict with keys:
                - success (bool): Whether the post succeeded.
                - posted_url (str): URL of the posted tweet.
                - error (str): Error message if failed.
                - dry_run (bool): Whether this was a dry run.
        """
        from .poster import XPoster

        poster = XPoster(profile_path=profile_path)
        dry_run = self._config.get("dry_run", True)
        return poster.post(body=body, dry_run=dry_run)

    def teardown(self) -> None:
        """Clean up resources."""
        if self._event_bus is not None:
            try:
                self._event_bus.unsubscribe(
                    "tier3.post", self.on_post
                )
            except ValueError:
                pass
        self._event_bus = None
        self._config = {}
        logger.info("XPosterExtension teardown complete")
