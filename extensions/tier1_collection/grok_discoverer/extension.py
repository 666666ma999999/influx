"""Grok Discovery extension.

Subscribes to influencer.discover to find new investment influencer
candidates using Grok API (xai-sdk x_search).
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict

from core.registry import Extension

logger = logging.getLogger(__name__)


class GrokDiscovererExtension(Extension):
    """Extension that discovers new influencer candidates via Grok API.

    On influencer.discover, uses GrokClient to search for investment
    influencers by keywords and network analysis.
    """

    def __init__(self) -> None:
        self._client = None
        self._event_bus = None
        self._output_dir = "output/research"

    @property
    def name(self) -> str:
        return "tier1.grok_discoverer"

    def setup(self, context: Any) -> None:
        """Initialize GrokClient and subscribe to influencer.discover.

        Args:
            context: Dict with event_bus, config, and registry.
        """
        from collector.grok_client import GrokClient

        config = context.get("config") if isinstance(context, dict) else getattr(context, "config", None)
        if config and isinstance(config, dict):
            ext_config = config.get("tier1.grok_discoverer", {})
            self._output_dir = ext_config.get("output_dir", self._output_dir)

        try:
            self._client = GrokClient()
        except ValueError:
            logger.warning("GrokClient初期化失敗（XAI_API_KEY未設定の可能性）")
            self._client = None

        self._event_bus = context.get("event_bus") if isinstance(context, dict) else getattr(context, "event_bus", None)

        if self._event_bus is not None:
            self._event_bus.subscribe(
                "influencer.discover", self.on_influencer_discover, priority=100
            )

        logger.info("GrokDiscovererExtension setup complete (output_dir=%s)", self._output_dir)

    def on_influencer_discover(self, event: str, payload: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
        """Handle influencer.discover event.

        Args:
            event: Event name (influencer.discover).
            payload: Must contain "keywords" (list[str]) or "existing_handles" (list[str]).
            meta: Event metadata.

        Returns:
            Dict with discovered candidates count and output path.
        """
        if self._client is None:
            logger.error("GrokClientが初期化されていません")
            return {"candidates_count": 0, "error": "GrokClient not initialized"}

        keywords = payload.get("keywords", [])
        existing_handles = payload.get("existing_handles", [])
        max_candidates = payload.get("max_candidates", 50)
        excluded_handles = payload.get("excluded_handles", [])

        all_candidates = []
        errors = []

        # Keyword-based discovery
        if keywords:
            result = self._client.discover_by_keywords(
                keywords=keywords,
                max_candidates=max_candidates,
                excluded_handles=excluded_handles,
            )
            all_candidates.extend(result.get("candidates", []))
            errors.extend(result.get("errors", []))

        # Network-based discovery
        if existing_handles:
            result = self._client.discover_by_network(
                existing_handles=existing_handles,
                max_candidates=max_candidates,
                excluded_handles=excluded_handles,
            )
            all_candidates.extend(result.get("candidates", []))
            errors.extend(result.get("errors", []))

        # Deduplicate by username
        seen = set()
        unique_candidates = []
        for candidate in all_candidates:
            username = candidate.get("username", "").lower()
            if username and username not in seen:
                seen.add(username)
                unique_candidates.append(candidate)

        # Save results
        output_path = self._save_discovery(unique_candidates, errors, keywords, existing_handles)

        # Emit event
        if self._event_bus is not None and unique_candidates:
            self._event_bus.publish(
                "influencer.candidates_found",
                {"candidates": unique_candidates, "output_path": output_path},
                meta,
            )

        logger.info("候補発見完了: %d件", len(unique_candidates))
        return {"candidates_count": len(unique_candidates), "output_path": output_path}

    def _save_discovery(self, candidates, errors, keywords, existing_handles):
        """Save discovery results to JSON file."""
        os.makedirs(self._output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"discovery_{timestamp}.json"
        output_path = os.path.join(self._output_dir, filename)

        data = {
            "discovered_at": datetime.now().isoformat(),
            "keywords": keywords,
            "existing_handles": existing_handles,
            "candidates_count": len(candidates),
            "candidates": candidates,
            "errors": errors,
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        logger.info("Discovery結果保存: %s (%d件)", output_path, len(candidates))
        return output_path

    def teardown(self) -> None:
        """Clean up resources."""
        if self._event_bus is not None:
            try:
                self._event_bus.unsubscribe("influencer.discover", self.on_influencer_discover)
            except ValueError:
                pass
        self._client = None
        self._event_bus = None
        logger.info("GrokDiscovererExtension teardown complete")
