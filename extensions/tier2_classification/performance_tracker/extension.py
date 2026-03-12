"""Performance tracker extension.

Subscribes to classify.post to detect recommendation tweets,
extracts tickers, and records them in the recommendation store
for performance tracking over time.
"""

import logging
from datetime import datetime
from typing import Any, Dict

from core.registry import Extension

logger = logging.getLogger(__name__)

# Categories that indicate a recommendation
TARGET_CATEGORIES = {"recommended_assets", "purchased_assets"}


class PerformanceTrackerExtension(Extension):
    """Extension that tracks influencer recommendation performance.

    On classify.post, inspects classified tweets for recommendation
    categories, extracts tickers, fetches prices, and records
    recommendations in the JSONL store.
    """

    def __init__(self) -> None:
        self._extractor = None
        self._fetcher = None
        self._store = None
        self._event_bus = None
        self._output_dir = "output/performance"

    @property
    def name(self) -> str:
        return "tier2.performance_tracker"

    def setup(self, context: Any) -> None:
        """Initialize components and subscribe to classify.post.

        Args:
            context: Dict with event_bus, config, and registry.
        """
        from collector.ticker_extractor import TickerExtractor
        from collector.price_fetcher import PriceFetcher
        from extensions.tier2_classification.performance_tracker.store import RecommendationStore

        # Read config
        config = context.get("config") if isinstance(context, dict) else getattr(context, "config", None)
        if config and isinstance(config, dict):
            ext_config = config.get("tier2.performance_tracker", {})
            self._output_dir = ext_config.get("output_dir", self._output_dir)
            cache_file = ext_config.get("price_cache_file", "output/price_cache.json")
        else:
            cache_file = "output/price_cache.json"

        self._extractor = TickerExtractor()
        self._fetcher = PriceFetcher(cache_file=cache_file)
        self._store = RecommendationStore(base_dir=self._output_dir)

        self._event_bus = context.get("event_bus") if isinstance(context, dict) else getattr(context, "event_bus", None)

        if self._event_bus is not None:
            self._event_bus.subscribe(
                "classify.post", self.on_classify_post, priority=200
            )

        logger.info("PerformanceTrackerExtension setup complete (output_dir=%s)", self._output_dir)

    def on_classify_post(self, event: str, payload: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
        """Handle classify.post event to detect and record recommendations.

        Args:
            event: Event name (classify.post).
            payload: Must contain a "tweet" key with classified tweet data.
            meta: Event metadata (correlation_id etc.).

        Returns:
            Dict with newly registered recommendations count.
        """
        tweet = payload.get("tweet", {})
        if not tweet:
            return {"registered": 0}

        # Check if tweet has recommendation categories
        categories = set(tweet.get("llm_categories", []))
        if not categories:
            categories = set(tweet.get("categories", []))

        if not categories & TARGET_CATEGORIES:
            return {"registered": 0}

        # Extract tickers
        tickers = self._extractor.extract(tweet)
        if not tickers:
            return {"registered": 0}

        # Get recommendation date
        posted_at = tweet.get("posted_at", "")
        rec_date = self._parse_date(posted_at)

        registered = 0
        for ticker_info in tickers:
            ticker = ticker_info["ticker"]
            tweet_url = tweet.get("url", "")

            if not tweet_url:
                continue

            rec_id = self._store.get_rec_id(tweet_url, ticker)

            # Fetch price at recommendation date
            price_at_rec = None
            if rec_date:
                price_data = self._fetcher.get_price_at_date(ticker, rec_date)
                price_at_rec = price_data.get("close")

            rec = {
                "rec_id": rec_id,
                "tweet_url": tweet_url,
                "ticker": ticker,
                "matched_text": ticker_info.get("matched_text", ""),
                "extraction_source": ticker_info.get("source", ""),
                "influencer": tweet.get("username", ""),
                "display_name": tweet.get("display_name", ""),
                "categories": list(categories & TARGET_CATEGORIES),
                "is_contrarian": tweet.get("is_contrarian", False),
                "recommended_at": rec_date,
                "price_at_recommendation": price_at_rec,
                "registered_at": datetime.now().isoformat(),
            }

            if self._store.add_if_new(rec):
                registered += 1
                logger.info(
                    "新規推奨登録: %s $%s by @%s",
                    rec_id[:8], ticker, rec.get("influencer", "?"),
                )

                # Emit recommendation.created event
                if self._event_bus is not None:
                    self._event_bus.publish(
                        "recommendation.created",
                        {"recommendation": rec},
                        meta,
                    )

        return {"registered": registered}

    def teardown(self) -> None:
        """Clean up resources."""
        if self._event_bus is not None:
            try:
                self._event_bus.unsubscribe("classify.post", self.on_classify_post)
            except ValueError:
                pass
        self._extractor = None
        self._fetcher = None
        self._store = None
        self._event_bus = None
        logger.info("PerformanceTrackerExtension teardown complete")

    @staticmethod
    def _parse_date(posted_at: str) -> str:
        """Parse posted_at to YYYY-MM-DD format.

        Args:
            posted_at: ISO 8601 date string or similar format.

        Returns:
            Date string in YYYY-MM-DD format, or None if unparseable.
        """
        if not posted_at:
            return None

        try:
            dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            if len(posted_at) >= 10:
                return posted_at[:10]
            return None
