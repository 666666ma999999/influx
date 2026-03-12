"""Tier 3 scheduler extension.

Determines optimal publish timing for news items based on configurable
strategies: immediate posting, optimal-time selection, or queue-based
scheduling with peak engagement hours on Japanese X (Twitter).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.registry import Extension

logger = logging.getLogger(__name__)

# Peak engagement hours on Japanese X (Twitter), in JST.
OPTIMAL_HOURS: List[int] = [7, 8, 12, 18, 20, 21]

# JST timezone offset (+09:00)
JST = timezone(timedelta(hours=9))


class SchedulerExtension(Extension):
    """Extension that schedules news items for posting.

    Supports three scheduling strategies:
    - immediate: Post right now.
    - optimal_time: Schedule for the next optimal engagement hour.
    - queue: Add to an internal queue with the next available slot.

    On setup, stores config and subscribes to the tier3.schedule hook.
    """

    def __init__(self) -> None:
        self._event_bus = None
        self._config: Dict[str, Any] = {}
        self._queue: List[Dict[str, Any]] = []
        self._last_scheduled_at: Optional[datetime] = None

    @property
    def name(self) -> str:
        return "tier3.scheduler"

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
                "tier3.schedule", self.on_schedule, priority=100
            )

        logger.info("SchedulerExtension setup complete")

    def on_schedule(
        self, event: str, payload: Dict[str, Any], meta: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Schedule a news item for posting.

        Args:
            event: Event name (tier3.schedule).
            payload: Must contain:
                - news_item (dict, required): News item conforming to
                  news_item.schema.json.
                - strategy (str, optional): Scheduling strategy. One of
                  "immediate", "optimal_time", "queue". Defaults to
                  "immediate".
            meta: Event metadata (correlation_id etc.).

        Returns:
            Dict with the updated news_item including scheduled_at and
            status fields.
        """
        news_item = payload.get("news_item")
        if not news_item:
            error_result = {
                "status": "error",
                "error_message": "news_item is required in payload",
                "news_item": None,
            }
            if self._event_bus is not None:
                self._event_bus.publish("schedule.error", error_result, meta)
            return error_result

        default_strategy = self._config.get("default_strategy", "immediate")
        strategy = payload.get("strategy", default_strategy)

        if strategy not in ("immediate", "optimal_time", "queue"):
            logger.warning(
                "Unknown strategy '%s', falling back to 'immediate'",
                strategy,
            )
            strategy = "immediate"

        now = datetime.now(tz=JST)

        try:
            if strategy == "immediate":
                news_item["scheduled_at"] = now.isoformat()
                news_item["status"] = "scheduled"
                logger.info(
                    "Scheduled news item immediately at %s",
                    news_item["scheduled_at"],
                )

            elif strategy == "optimal_time":
                optimal_slot = self._find_next_optimal_slot()
                news_item["scheduled_at"] = optimal_slot
                news_item["status"] = "scheduled"
                logger.info(
                    "Scheduled news item for optimal time: %s",
                    optimal_slot,
                )

            elif strategy == "queue":
                optimal_slot = self._find_next_optimal_slot()
                news_item["scheduled_at"] = optimal_slot
                news_item["status"] = "scheduled"
                self._queue.append(news_item)
                logger.info(
                    "Queued news item for %s (queue size: %d)",
                    optimal_slot,
                    len(self._queue),
                )

            # Track the last scheduled time for queue spacing
            self._last_scheduled_at = datetime.fromisoformat(
                news_item["scheduled_at"]
            )

        except Exception as exc:
            logger.exception("Scheduling failed for news item")
            error_result = {
                "status": "error",
                "error_message": str(exc),
                "news_item": news_item,
            }
            if self._event_bus is not None:
                self._event_bus.publish("schedule.error", error_result, meta)
            return error_result

        result = {
            "status": "success",
            "strategy": strategy,
            "news_item": news_item,
        }

        if self._event_bus is not None:
            self._event_bus.publish(
                "schedule.completed",
                {
                    "news_item": news_item,
                    "strategy": strategy,
                    "scheduled_at": news_item["scheduled_at"],
                },
                meta,
            )

        return result

    def _find_next_optimal_slot(self) -> str:
        """Find the next optimal posting time slot.

        Considers OPTIMAL_HOURS for peak engagement and respects the
        min_interval_minutes config to avoid posting too frequently.

        Returns:
            ISO 8601 formatted datetime string in JST.
        """
        optimal_hours = self._config.get("optimal_hours", OPTIMAL_HOURS)
        min_interval = self._config.get("min_interval_minutes", 60)

        now = datetime.now(tz=JST)

        # Determine earliest allowed time based on min_interval
        if self._last_scheduled_at is not None:
            earliest = self._last_scheduled_at + timedelta(minutes=min_interval)
            if earliest > now:
                candidate_start = earliest
            else:
                candidate_start = now
        else:
            candidate_start = now

        # Search for the next optimal hour starting from candidate_start
        # Check today first, then tomorrow
        for day_offset in range(2):
            candidate_date = candidate_start.date() + timedelta(days=day_offset)
            for hour in sorted(optimal_hours):
                candidate = datetime(
                    year=candidate_date.year,
                    month=candidate_date.month,
                    day=candidate_date.day,
                    hour=hour,
                    minute=0,
                    second=0,
                    tzinfo=JST,
                )
                if candidate > candidate_start:
                    return candidate.isoformat()

        # Fallback: next day first optimal hour (should not normally reach here)
        fallback_date = candidate_start.date() + timedelta(days=2)
        fallback_hour = sorted(optimal_hours)[0]
        fallback = datetime(
            year=fallback_date.year,
            month=fallback_date.month,
            day=fallback_date.day,
            hour=fallback_hour,
            minute=0,
            second=0,
            tzinfo=JST,
        )
        return fallback.isoformat()

    def teardown(self) -> None:
        """Clean up resources."""
        if self._event_bus is not None:
            try:
                self._event_bus.unsubscribe(
                    "tier3.schedule", self.on_schedule
                )
            except ValueError:
                pass
        self._event_bus = None
        self._config = {}
        self._queue.clear()
        self._last_scheduled_at = None
        logger.info("SchedulerExtension teardown complete")
