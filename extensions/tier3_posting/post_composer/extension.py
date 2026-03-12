"""Post composer extension.

Composes news items from curated tweets with title, body, and hashtags.
Subscribes to tier3.compose events and produces news_item dicts conforming
to influx://contracts/news_item.schema.json.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from core.registry import Extension

logger = logging.getLogger(__name__)

# Category key -> Japanese hashtags mapping
CATEGORY_HASHTAGS: Dict[str, List[str]] = {
    "recommended_assets": ["#おすすめ銘柄", "#投資"],
    "purchased_assets": ["#購入報告", "#投資"],
    "sold_assets": ["#売却報告", "#投資"],
    "winning_trades": ["#勝ちトレード", "#投資"],
    "ipo": ["#IPO", "#新規公開"],
    "market_trend": ["#市況", "#マーケット"],
    "bullish_assets": ["#高騰", "#株式"],
    "bearish_assets": ["#下落", "#株式"],
    "warning_signals": ["#警戒", "#投資注意"],
}

# Category key -> Japanese display name mapping
CATEGORY_DISPLAY_NAMES: Dict[str, str] = {
    "recommended_assets": "おすすめ銘柄",
    "purchased_assets": "購入報告",
    "sold_assets": "売却報告",
    "winning_trades": "勝ちトレード",
    "ipo": "IPO",
    "market_trend": "市況トレンド",
    "bullish_assets": "高騰銘柄",
    "bearish_assets": "下落銘柄",
    "warning_signals": "警戒シグナル",
}


class PostComposerExtension(Extension):
    """Extension that composes news items from curated tweets.

    Subscribes to tier3.compose events emitted by the news_curator extension,
    and produces structured news_item dicts with title, body, hashtags, and
    metadata suitable for posting to X (Twitter).
    """

    def __init__(self) -> None:
        self._event_bus = None
        self._config: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "tier3.post_composer"

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
                "tier3.compose", self.on_compose, priority=100
            )

        logger.info("PostComposerExtension setup complete")

    def on_compose(
        self, event: str, payload: Dict[str, Any], meta: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Compose a news item from curated tweets.

        Args:
            event: Event name (tier3.compose).
            payload: Must contain:
                - curated_items (list[dict], required): Curated tweet items
                  from news_curator.
                - format (str, optional): Output format - "x_post", "thread",
                  or "summary_card". Defaults to config default_format or "x_post".
                - category (str, required): News category key
                  (e.g. "market_trend").
                - corner_name (str, optional): Display name for the news corner.
            meta: Event metadata (correlation_id etc.).

        Returns:
            Dict conforming to news_item.schema.json with keys:
            news_id, source_items, title, body, format, scheduled_at,
            hashtags, status, metadata.
        """
        curated_items = payload.get("curated_items", [])
        if not curated_items:
            error_result: Dict[str, Any] = {
                "status": "error",
                "error_message": "curated_items is required and must not be empty",
            }
            if self._event_bus is not None:
                self._event_bus.publish("compose.error", error_result, meta)
            return error_result

        category = payload.get("category", "")
        corner_name = payload.get(
            "corner_name",
            CATEGORY_DISPLAY_NAMES.get(category, category),
        )
        output_format = payload.get(
            "format",
            self._config.get("default_format", "x_post"),
        )

        # Generate unique news_id
        news_id = str(uuid.uuid4())

        # Extract source tweet IDs from curated items
        source_items = self._extract_source_ids(curated_items)

        # Build title
        title = self._build_title(category, corner_name, curated_items)

        # Build body based on format
        if output_format == "thread":
            body_result = self._build_thread(curated_items, category)
        elif output_format == "summary_card":
            body_result = self._build_summary_card(curated_items, category)
        else:
            body_result = self._build_x_post(curated_items, category)

        body = body_result.get("body", "")

        # Extract hashtags from category
        hashtags = self._category_to_hashtags(category)

        # Set scheduled_at to current time (will be overridden by scheduler)
        scheduled_at = datetime.now(timezone.utc).isoformat()

        news_item: Dict[str, Any] = {
            "news_id": news_id,
            "source_items": source_items,
            "title": title,
            "body": body,
            "format": output_format,
            "scheduled_at": scheduled_at,
            "hashtags": hashtags,
            "status": "draft",
            "metadata": {
                "category": category,
                "corner_name": corner_name,
                "item_count": len(curated_items),
                "composer_version": "1.0.0",
            },
        }

        # Publish compose.completed event
        if self._event_bus is not None:
            self._event_bus.publish("compose.completed", news_item, meta)

        logger.info(
            "Composed news item %s (format=%s, category=%s, items=%d)",
            news_id,
            output_format,
            category,
            len(curated_items),
        )

        return news_item

    def _extract_source_ids(self, curated_items: List[Dict[str, Any]]) -> List[str]:
        """Extract tweet IDs or URLs from curated items.

        Args:
            curated_items: List of curated tweet dicts.

        Returns:
            List of tweet_id strings.
        """
        source_ids: List[str] = []
        for item in curated_items:
            tweet_id = item.get("tweet_id") or item.get("url", "")
            if tweet_id:
                source_ids.append(str(tweet_id))
        return source_ids or ["unknown"]

    def _build_title(
        self,
        category: str,
        corner_name: str,
        curated_items: List[Dict[str, Any]],
    ) -> str:
        """Build a news item title from category and top tweet info.

        Args:
            category: Category key (e.g. "market_trend").
            corner_name: Display name for the corner.
            curated_items: List of curated tweet dicts.

        Returns:
            Title string.
        """
        display_name = corner_name or CATEGORY_DISPLAY_NAMES.get(category, category)
        top_username = ""
        if curated_items:
            top_username = curated_items[0].get("username", "")

        if top_username:
            return f"【{display_name}】@{top_username} 他の注目ツイート"
        return f"【{display_name}】本日の注目ツイート"

    def _build_x_post(
        self, curated_items: List[Dict[str, Any]], category: str
    ) -> Dict[str, str]:
        """Build compact single-tweet format body (max 280 chars).

        Args:
            curated_items: List of curated tweet dicts.
            category: Category key.

        Returns:
            Dict with "body" key.
        """
        max_body_length = self._config.get("max_body_length", 280)
        include_urls = self._config.get("include_source_urls", True)

        if not curated_items:
            return {"body": ""}

        top_item = curated_items[0]
        text = top_item.get("text", "")
        username = top_item.get("username", "")
        url = top_item.get("url", "")

        # Build body parts
        parts: List[str] = []
        if username:
            parts.append(f"@{username}")
        if text:
            parts.append(text)

        body = "\n".join(parts)

        # Append source URL if configured
        if include_urls and url:
            url_suffix = f"\n{url}"
            available_length = max_body_length - len(url_suffix)
            if len(body) > available_length:
                body = body[:available_length - 1] + "..."
            body += url_suffix
        else:
            if len(body) > max_body_length:
                body = body[:max_body_length - 1] + "..."

        return {"body": body}

    def _build_thread(
        self, curated_items: List[Dict[str, Any]], category: str
    ) -> Dict[str, str]:
        """Build multi-tweet thread format with numbered items.

        Args:
            curated_items: List of curated tweet dicts.
            category: Category key.

        Returns:
            Dict with "body" key.
        """
        include_urls = self._config.get("include_source_urls", True)
        display_name = CATEGORY_DISPLAY_NAMES.get(category, category)

        lines: List[str] = [f"【{display_name}まとめ】\n"]

        for i, item in enumerate(curated_items, start=1):
            username = item.get("username", "")
            text = item.get("text", "")
            url = item.get("url", "")

            entry = f"{i}. "
            if username:
                entry += f"@{username}: "
            if text:
                # Truncate individual item text for readability
                truncated = text[:200] + "..." if len(text) > 200 else text
                entry += truncated

            if include_urls and url:
                entry += f"\n   {url}"

            lines.append(entry)

        body = "\n\n".join(lines)
        return {"body": body}

    def _build_summary_card(
        self, curated_items: List[Dict[str, Any]], category: str
    ) -> Dict[str, str]:
        """Build bullet-point summary format.

        Args:
            curated_items: List of curated tweet dicts.
            category: Category key.

        Returns:
            Dict with "body" key.
        """
        include_urls = self._config.get("include_source_urls", True)
        display_name = CATEGORY_DISPLAY_NAMES.get(category, category)

        lines: List[str] = [f"{display_name} サマリー"]
        lines.append("=" * 30)

        for item in curated_items:
            username = item.get("username", "")
            text = item.get("text", "")
            url = item.get("url", "")

            # Create bullet point with truncated text
            truncated = text[:150] + "..." if len(text) > 150 else text
            bullet = f"- "
            if username:
                bullet += f"@{username}: "
            bullet += truncated

            if include_urls and url:
                bullet += f"\n  ({url})"

            lines.append(bullet)

        body = "\n".join(lines)
        return {"body": body}

    def _category_to_hashtags(self, category: str) -> List[str]:
        """Map category key to Japanese hashtags.

        Args:
            category: Category key (e.g. "market_trend").

        Returns:
            List of hashtag strings.
        """
        return CATEGORY_HASHTAGS.get(category, ["#株式投資", "#投資"])

    def teardown(self) -> None:
        """Clean up resources."""
        if self._event_bus is not None:
            try:
                self._event_bus.unsubscribe(
                    "tier3.compose", self.on_compose
                )
            except ValueError:
                pass
        self._event_bus = None
        self._config = {}
        logger.info("PostComposerExtension teardown complete")
