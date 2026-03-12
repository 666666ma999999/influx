"""ML classifier extension.

Wraps the existing collector.ml_classifier.MLClassifier as an extension
so it can participate in the tier2 classification pipeline via the EventBus.
"""

import logging
from typing import Any, Dict, List

from core.registry import Extension

logger = logging.getLogger(__name__)


class MLClassifierExtension(Extension):
    """Extension wrapper around the TF-IDF + LogisticRegression MLClassifier.

    On setup, imports and instantiates the existing MLClassifier and loads
    the trained model files.
    On the tier2.classify hook, classifies the tweet and emits
    classification.created via the EventBus.
    """

    def __init__(self) -> None:
        self._classifier = None
        self._event_bus = None

    @property
    def name(self) -> str:
        return "tier2.ml_classifier"

    def setup(self, context: Any) -> None:
        """Import, instantiate, and load the existing MLClassifier.

        Args:
            context: Dict with event_bus, config, and registry.
        """
        from collector.ml_classifier import MLClassifier

        config = context.get("config") if isinstance(context, dict) else getattr(context, "config", None)
        model_dir = "models"
        if isinstance(config, dict):
            model_dir = config.get("model_dir", model_dir)

        self._classifier = MLClassifier(model_dir=model_dir)
        self._classifier.load()
        self._event_bus = context.get("event_bus") if isinstance(context, dict) else getattr(context, "event_bus", None)

        if self._event_bus is not None:
            self._event_bus.subscribe(
                "tier2.classify", self.on_classify, priority=300
            )

        logger.info("MLClassifierExtension setup complete")

    def on_classify(self, event: str, payload: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
        """Classify a tweet using TF-IDF + LogisticRegression model.

        Args:
            event: Event name (tier2.classify).
            payload: Must contain a "tweet" key with tweet data,
                     or a "tweets" key with a list of tweet data.
            meta: Event metadata (correlation_id etc.).

        Returns:
            Classification result dict.
        """
        tweets: List[Dict[str, Any]] = []
        if "tweets" in payload:
            tweets = [dict(tw) for tw in payload["tweets"]]
        elif "tweet" in payload:
            tweet = payload.get("tweet", {})
            if tweet:
                tweets = [dict(tweet)]

        if not tweets:
            return {}

        if len(tweets) == 1:
            result = self._classifier.classify(tweets[0])
            results = [result]
        else:
            results = self._classifier.classify_batch(tweets)

        if self._event_bus is not None:
            for result in results:
                self._event_bus.publish(
                    "classification.created",
                    {
                        "tweet": result,
                        "classifier": self.name,
                        "ml_categories": result.get("ml_categories", []),
                        "ml_confidence": result.get("ml_confidence", 0.0),
                    },
                    meta,
                )

        if len(results) == 1:
            return {
                "ml_categories": results[0].get("ml_categories", []),
                "ml_confidence": results[0].get("ml_confidence", 0.0),
            }

        return {
            "results": [
                {
                    "ml_categories": r.get("ml_categories", []),
                    "ml_confidence": r.get("ml_confidence", 0.0),
                }
                for r in results
            ],
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
        logger.info("MLClassifierExtension teardown complete")
