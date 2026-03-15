"""Impression tracker extension.

Tracks engagement metrics (views, likes, retweets, replies, bookmarks) for
posted tweets by scraping individual tweet pages via Playwright.  Playwright
is lazy-imported only when scraping is actually triggered.
"""

import logging
from typing import Any, Dict

from core.registry import Extension

logger = logging.getLogger(__name__)


class ImpressionTrackerExtension(Extension):
    """Extension that tracks impressions for posted tweets.

    On setup, stores config and subscribes to the tier3.track_impressions hook.
    Playwright is lazy-imported only when scraping is performed.
    """

    def __init__(self) -> None:
        self._event_bus = None
        self._config: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "tier3.impression_tracker"

    def setup(self, context: Any) -> None:
        """Extract event_bus and config from context.

        Playwright is NOT imported here because it requires a running
        browser runtime.  It will be lazy-imported in ImpressionScraper.

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
                "tier3.track_impressions",
                self.on_track_impressions,
                priority=100,
            )

        logger.info("ImpressionTrackerExtension setup complete")

    def on_track_impressions(
        self, event: str, payload: Dict[str, Any], meta: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Track impressions for a posted tweet.

        Args:
            event: Event name (tier3.track_impressions).
            payload: Must contain:
                - tweet_url (str, required): URL of the posted tweet.
                - profile_path (str, optional): Browser profile path override.
            meta: Event metadata (correlation_id etc.).

        Returns:
            Dict with scraped engagement metrics or error information.
        """
        tweet_url = payload.get("tweet_url")
        if not tweet_url:
            error_result = {
                "status": "error",
                "error_message": "tweet_url is required in payload",
            }
            if self._event_bus is not None:
                self._event_bus.publish("impression.failed", error_result, meta)
            return error_result

        profile_path = payload.get(
            "profile_path",
            self._config.get("profile_path", "./x_profile"),
        )

        try:
            from .scraper import ImpressionScraper

            scraper = ImpressionScraper(profile_path=profile_path)
            result = scraper.scrape(tweet_url)

            if result.get("impressions") is not None:
                tracked_result = {
                    "status": "success",
                    "tweet_url": tweet_url,
                    "metrics": result,
                }
                if self._event_bus is not None:
                    self._event_bus.publish(
                        "impression.tracked", tracked_result, meta
                    )
                return tracked_result

            error_result = {
                "status": "error",
                "tweet_url": tweet_url,
                "error_message": result.get("error", "Unknown scraping error"),
            }
            if self._event_bus is not None:
                self._event_bus.publish("impression.failed", error_result, meta)
            return error_result

        except Exception as exc:
            logger.exception(
                "Impression tracking failed for %s", tweet_url
            )
            error_result = {
                "status": "error",
                "tweet_url": tweet_url,
                "error_message": str(exc),
            }
            if self._event_bus is not None:
                self._event_bus.publish("impression.failed", error_result, meta)
            return error_result

    def teardown(self) -> None:
        """Clean up resources."""
        if self._event_bus is not None:
            try:
                self._event_bus.unsubscribe(
                    "tier3.track_impressions", self.on_track_impressions
                )
            except ValueError:
                pass
        self._event_bus = None
        self._config = {}
        logger.info("ImpressionTrackerExtension teardown complete")
