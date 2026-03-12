"""News curator extension for Tier 3 posting pipeline.

Selects and ranks classified tweets for news item composition.
Filters by category, confidence threshold, and time window, then
groups by category and selects top items per category.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.registry import Extension

logger = logging.getLogger(__name__)

# Maximum length for the text snippet in curated items
_SNIPPET_MAX_LEN = 140


class NewsCuratorExtension(Extension):
    """Curates classified tweets into ranked news items.

    On setup, stores config and subscribes to the tier3.curate hook.
    When the hook fires, filters, ranks, and groups tweets, then
    publishes a curation.completed event with the curated items.
    """

    def __init__(self) -> None:
        self._event_bus: Optional[Any] = None
        self._config: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "tier3.news_curator"

    def setup(self, context: Any) -> None:
        """Extract event_bus and config from context.

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
                "tier3.curate", self.on_curate, priority=100
            )

        logger.info("NewsCuratorExtension setup complete")

    # ------------------------------------------------------------------
    # Hook handler
    # ------------------------------------------------------------------

    def on_curate(
        self, event: str, payload: Dict[str, Any], meta: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Curate classified tweets into ranked news items.

        Args:
            event: Event name (tier3.curate).
            payload: Must contain:
                - tweets (list[dict], required): Classified tweets with
                  categories, llm_categories, ensemble_categories, etc.
                - category_filter (list[str], optional): Categories to
                  include. Defaults to all categories.
                - min_confidence (float, optional): Minimum confidence
                  threshold. Defaults to 0.5.
                - max_items (int, optional): Maximum total curated items.
                  Defaults to 10.
                - time_window_hours (int, optional): Only consider tweets
                  from the last N hours. Defaults to 24.
            meta: Event metadata (correlation_id etc.).

        Returns:
            Dict with curated_items (list), total_candidates (int),
            and selected_count (int).
        """
        tweets: List[Dict[str, Any]] = payload.get("tweets", [])
        category_filter: Optional[List[str]] = payload.get("category_filter")
        min_confidence: float = payload.get(
            "min_confidence",
            self._config.get("min_confidence", 0.5),
        )
        max_items: int = payload.get(
            "max_items",
            self._config.get("max_total_items", 10),
        )
        time_window_hours: int = payload.get(
            "time_window_hours",
            self._config.get("time_window_hours", 24),
        )
        max_items_per_category: int = self._config.get(
            "max_items_per_category", 5
        )

        total_candidates = len(tweets)

        # Step 1: Filter by time window
        tweets = self._filter_by_time_window(tweets, time_window_hours)

        # Step 2: Filter by category
        tweets = self._filter_by_category(tweets, category_filter)

        # Step 3: Filter by confidence
        tweets = self._filter_by_confidence(tweets, min_confidence)

        # Step 4: Sort by confidence desc, then like_count desc
        tweets = self._sort_tweets(tweets)

        # Step 5: Group by category and select top N per category
        curated_items = self._group_and_select(
            tweets, max_items_per_category, max_items
        )

        result: Dict[str, Any] = {
            "curated_items": curated_items,
            "total_candidates": total_candidates,
            "selected_count": len(curated_items),
        }

        if self._event_bus is not None:
            self._event_bus.publish("curation.completed", result, meta)

        logger.info(
            "Curation complete: %d/%d tweets selected",
            len(curated_items),
            total_candidates,
        )

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_confidence(tweet: Dict[str, Any]) -> float:
        """Extract the best available confidence score from a tweet.

        Checks ensemble_confidence first, then llm_confidence.

        Args:
            tweet: Tweet dict with classification fields.

        Returns:
            Confidence score as float (0.0 if not available).
        """
        confidence = tweet.get("ensemble_confidence")
        if confidence is not None:
            return float(confidence)
        confidence = tweet.get("llm_confidence")
        if confidence is not None:
            return float(confidence)
        return 0.0

    @staticmethod
    def _get_categories(tweet: Dict[str, Any]) -> List[str]:
        """Extract the best available category list from a tweet.

        Checks ensemble_categories first, then llm_categories,
        then categories (keyword-based).

        Args:
            tweet: Tweet dict with classification fields.

        Returns:
            List of category strings.
        """
        for key in ("ensemble_categories", "llm_categories", "categories"):
            cats = tweet.get(key)
            if cats:
                return list(cats)
        return []

    @staticmethod
    def _filter_by_time_window(
        tweets: List[Dict[str, Any]], time_window_hours: int
    ) -> List[Dict[str, Any]]:
        """Filter tweets to those within the time window.

        Args:
            tweets: List of tweet dicts.
            time_window_hours: Number of hours to look back.

        Returns:
            Filtered list of tweets.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=time_window_hours)
        filtered: List[Dict[str, Any]] = []

        for tweet in tweets:
            posted_at = tweet.get("posted_at")
            if posted_at is None:
                # Include tweets without a timestamp (cannot determine age)
                filtered.append(tweet)
                continue
            try:
                if isinstance(posted_at, str):
                    dt = datetime.fromisoformat(
                        posted_at.replace("Z", "+00:00")
                    )
                else:
                    dt = posted_at
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt >= cutoff:
                    filtered.append(tweet)
            except (ValueError, TypeError):
                # If parsing fails, include the tweet to avoid data loss
                filtered.append(tweet)

        return filtered

    def _filter_by_category(
        self,
        tweets: List[Dict[str, Any]],
        category_filter: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        """Filter tweets by category membership.

        Args:
            tweets: List of tweet dicts.
            category_filter: Categories to include, or None for all.

        Returns:
            Filtered list of tweets.
        """
        if not category_filter:
            return tweets

        filter_set = set(category_filter)
        return [
            t for t in tweets
            if filter_set.intersection(self._get_categories(t))
        ]

    def _filter_by_confidence(
        self, tweets: List[Dict[str, Any]], min_confidence: float
    ) -> List[Dict[str, Any]]:
        """Filter tweets by minimum confidence threshold.

        Args:
            tweets: List of tweet dicts.
            min_confidence: Minimum confidence score.

        Returns:
            Filtered list of tweets.
        """
        return [
            t for t in tweets
            if self._get_confidence(t) >= min_confidence
        ]

    def _sort_tweets(
        self, tweets: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Sort tweets by confidence descending, then like_count descending.

        Args:
            tweets: List of tweet dicts.

        Returns:
            Sorted list of tweets.
        """
        return sorted(
            tweets,
            key=lambda t: (
                self._get_confidence(t),
                t.get("like_count") or 0,
            ),
            reverse=True,
        )

    def _group_and_select(
        self,
        tweets: List[Dict[str, Any]],
        max_per_category: int,
        max_total: int,
    ) -> List[Dict[str, Any]]:
        """Group tweets by category and select top N per category.

        Each tweet may belong to multiple categories. It will be counted
        toward each category's quota but appear only once in the output.

        Args:
            tweets: Sorted list of tweet dicts.
            max_per_category: Maximum items per category.
            max_total: Maximum total items.

        Returns:
            List of curated item dicts.
        """
        category_counts: Dict[str, int] = defaultdict(int)
        seen_urls: set = set()
        curated: List[Dict[str, Any]] = []

        for tweet in tweets:
            if len(curated) >= max_total:
                break

            tweet_url = tweet.get("url", "")
            if tweet_url in seen_urls:
                continue

            categories = self._get_categories(tweet)
            if not categories:
                continue

            # Check if at least one category still has room
            has_room = any(
                category_counts[cat] < max_per_category
                for cat in categories
            )
            if not has_room:
                continue

            # Build curated item
            text = tweet.get("text", "")
            snippet = (
                text[:_SNIPPET_MAX_LEN] + "..."
                if len(text) > _SNIPPET_MAX_LEN
                else text
            )

            curated_item: Dict[str, Any] = {
                "tweet_url": tweet_url,
                "text_snippet": snippet,
                "categories": categories,
                "confidence": self._get_confidence(tweet),
                "like_count": tweet.get("like_count") or 0,
            }
            curated.append(curated_item)
            seen_urls.add(tweet_url)

            # Increment category counts
            for cat in categories:
                category_counts[cat] += 1

        return curated

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def teardown(self) -> None:
        """Clean up resources."""
        if self._event_bus is not None:
            try:
                self._event_bus.unsubscribe(
                    "tier3.curate", self.on_curate
                )
            except ValueError:
                pass
        self._event_bus = None
        self._config = {}
        logger.info("NewsCuratorExtension teardown complete")
