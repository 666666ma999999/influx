"""Ensemble classifier extension.

Wraps the existing collector.ensemble_classifier.EnsembleClassifier as an
extension so it can participate in the tier2 classification fusion pipeline
via the EventBus.

This extension subscribes to "tier2.classify.fusion" (not "tier2.classify")
because it runs AFTER all individual classifiers.  Priority 500 ensures
it runs last, consuming the categories / llm_categories / ml_categories
fields already attached to the tweet by upstream classifiers.
"""

import logging
from typing import Any, Dict

from core.registry import Extension

logger = logging.getLogger(__name__)


class EnsembleClassifierExtension(Extension):
    """Extension wrapper around the meta-learner EnsembleClassifier.

    On setup, imports and instantiates the existing EnsembleClassifier and
    calls .load() to restore the persisted meta-classifier model.
    On the tier2.classify.fusion hook, fuses per-classifier outputs into a
    final ensemble prediction and emits classification.created via the
    EventBus.
    """

    def __init__(self) -> None:
        self._classifier = None
        self._event_bus = None

    @property
    def name(self) -> str:
        return "tier2.ensemble_classifier"

    def setup(self, context: Any) -> None:
        """Import and instantiate the existing EnsembleClassifier.

        Args:
            context: Dict with event_bus, config, and registry.
        """
        from collector.ensemble_classifier import EnsembleClassifier

        config = context.get("config", {}) if isinstance(context, dict) else getattr(context, "config", {})
        model_dir = config.get("model_dir", "models") if isinstance(config, dict) else getattr(config, "model_dir", "models")

        self._classifier = EnsembleClassifier(model_dir=model_dir)
        self._classifier.load()

        self._event_bus = context.get("event_bus") if isinstance(context, dict) else getattr(context, "event_bus", None)

        if self._event_bus is not None:
            self._event_bus.subscribe(
                "tier2.classify.fusion", self.on_classify_fusion, priority=500
            )

        logger.info("EnsembleClassifierExtension setup complete")

    def on_classify_fusion(self, event: str, payload: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
        """Fuse individual classifier outputs into an ensemble prediction.

        Expects the tweet to already carry categories, llm_categories, and
        ml_categories fields from prior classifiers in the pipeline.

        Args:
            event: Event name (tier2.classify.fusion).
            payload: Must contain a "tweet" key with tweet data.
            meta: Event metadata (correlation_id etc.).

        Returns:
            Ensemble classification result dict.
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
                    "ensemble_categories": result.get("ensemble_categories", []),
                    "ensemble_confidence": result.get("ensemble_confidence", 0.0),
                },
                meta,
            )

        return {
            "ensemble_categories": result.get("ensemble_categories", []),
            "ensemble_confidence": result.get("ensemble_confidence", 0.0),
        }

    def teardown(self) -> None:
        """Clean up resources."""
        if self._event_bus is not None:
            try:
                self._event_bus.unsubscribe("tier2.classify.fusion", self.on_classify_fusion)
            except ValueError:
                pass
        self._classifier = None
        self._event_bus = None
        logger.info("EnsembleClassifierExtension teardown complete")
