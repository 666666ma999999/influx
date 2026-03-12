"""LLM classifier extension.

Wraps the existing collector.llm_classifier.LLMClassifier as an extension
so it can participate in the tier2 classification pipeline via the EventBus.
"""

import logging
from typing import Any, Dict, List

from core.registry import Extension

logger = logging.getLogger(__name__)


class LLMClassifierExtension(Extension):
    """Extension wrapper around the Claude API based LLMClassifier.

    On setup, imports and instantiates the existing LLMClassifier.
    On the tier2.classify hook, classifies the tweet(s) and emits
    classification.created via the EventBus.
    """

    def __init__(self) -> None:
        self._classifier = None
        self._event_bus = None

    @property
    def name(self) -> str:
        return "tier2.llm_classifier"

    def setup(self, context: Any) -> None:
        """Import and instantiate the existing LLMClassifier.

        Args:
            context: Dict with event_bus, config, and registry.
        """
        from collector.llm_classifier import LLMClassifier

        self._classifier = LLMClassifier()
        self._event_bus = context.get("event_bus") if isinstance(context, dict) else getattr(context, "event_bus", None)

        if self._event_bus is not None:
            self._event_bus.subscribe(
                "tier2.classify", self.on_classify, priority=200
            )

        logger.info("LLMClassifierExtension setup complete")

    def on_classify(self, event: str, payload: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
        """Classify tweet(s) using the Claude API.

        Supports two payload shapes:
        - Single tweet: payload contains a "tweet" key with tweet data.
        - Batch tweets: payload contains a "tweets" key with a list of tweets.

        Args:
            event: Event name (tier2.classify).
            payload: Must contain a "tweet" or "tweets" key.
            meta: Event metadata (correlation_id etc.).

        Returns:
            Classification result dict.
        """
        tweets_list: List[Dict[str, Any]] = payload.get("tweets", [])
        single_tweet = payload.get("tweet", {})

        # Single tweet mode
        if single_tweet and not tweets_list:
            results = self._classifier.classify_batch([dict(single_tweet)])
            if not results:
                return {}

            result = results[0]
            classification = {
                "llm_categories": result.get("llm_categories", []),
                "llm_reasoning": result.get("llm_reasoning", ""),
                "llm_confidence": result.get("llm_confidence", 0.0),
            }

            if self._event_bus is not None:
                self._event_bus.publish(
                    "classification.created",
                    {
                        "tweet": single_tweet,
                        "classifier": self.name,
                        "llm_categories": classification["llm_categories"],
                        "llm_reasoning": classification["llm_reasoning"],
                        "llm_confidence": classification["llm_confidence"],
                    },
                    meta,
                )

            return classification

        # Batch tweets mode
        if tweets_list:
            classified = self._classifier.classify_all([dict(t) for t in tweets_list])

            batch_results = []
            for tweet in classified:
                entry = {
                    "llm_categories": tweet.get("llm_categories", []),
                    "llm_reasoning": tweet.get("llm_reasoning", ""),
                    "llm_confidence": tweet.get("llm_confidence", 0.0),
                }
                batch_results.append(entry)

                if self._event_bus is not None:
                    self._event_bus.publish(
                        "classification.created",
                        {
                            "tweet": tweet,
                            "classifier": self.name,
                            "llm_categories": entry["llm_categories"],
                            "llm_reasoning": entry["llm_reasoning"],
                            "llm_confidence": entry["llm_confidence"],
                        },
                        meta,
                    )

            return {"results": batch_results}

        return {}

    def teardown(self) -> None:
        """Clean up resources."""
        if self._event_bus is not None:
            try:
                self._event_bus.unsubscribe("tier2.classify", self.on_classify)
            except ValueError:
                pass
        self._classifier = None
        self._event_bus = None
        logger.info("LLMClassifierExtension teardown complete")
