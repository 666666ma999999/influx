"""Keyword classifier extension.

Wraps the existing collector.classifier.TweetClassifier as an extension
so it can participate in the tier2 classification pipeline via the EventBus.
"""

import logging
from typing import Any, Dict

from core.registry import Extension

logger = logging.getLogger(__name__)


class KeywordClassifierExtension(Extension):
    """Extension wrapper around the keyword-based TweetClassifier.

    On setup, imports and instantiates the existing TweetClassifier.
    On the tier2.classify hook, classifies the tweet and emits
    classification.created via the EventBus.
    """

    def __init__(self) -> None:
        self._classifier = None
        self._event_bus = None

    @property
    def name(self) -> str:
        return "tier2.keyword_classifier"

    def setup(self, context: Any) -> None:
        """Import and instantiate the existing TweetClassifier.

        Args:
            context: Dict with event_bus, config, and registry.
        """
        from collector.classifier import TweetClassifier

        self._classifier = TweetClassifier()
        self._event_bus = context.get("event_bus") if isinstance(context, dict) else getattr(context, "event_bus", None)

        if self._event_bus is not None:
            self._event_bus.subscribe(
                "tier2.classify", self.on_classify, priority=100
            )

        logger.info("KeywordClassifierExtension setup complete")

    def on_classify(self, event: str, payload: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
        """Classify a tweet using keyword/regex matching.

        Args:
            event: Event name (tier2.classify).
            payload: Must contain a "tweet" key with tweet data.
            meta: Event metadata (correlation_id etc.).

        Returns:
            Classification result dict.
        """
        tweet = payload.get("tweet", {})
        if not tweet:
            return {}

        result = self._classifier.classify(dict(tweet))

        if self._event_bus is not None:
            self._event_bus.publish(
                "classification.created",
                {
                    "tweet": result,
                    "classifier": self.name,
                    "categories": result.get("categories", []),
                    "category_details": result.get("category_details", {}),
                },
                meta,
            )

        return {
            "categories": result.get("categories", []),
            "category_details": result.get("category_details", {}),
            "category_count": result.get("category_count", 0),
        }

    def teardown(self) -> None:
        """Clean up resources."""
        if self._event_bus is not None:
            try:
                self._event_bus.unsubscribe("tier2.classify", self.on_classify)
            except ValueError:
                pass
        self._classifier = None
        self._event_bus = None
        logger.info("KeywordClassifierExtension teardown complete")
