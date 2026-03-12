"""X (Twitter) collector extension.

Wraps the existing collector.x_collector.SafeXCollector as an extension
so it can participate in the tier1 collection pipeline via the EventBus.
"""

import logging
from typing import Any, Dict, List

from core.registry import Extension

logger = logging.getLogger(__name__)


class XCollectorExtension(Extension):
    """Extension wrapper around the Playwright-based SafeXCollector.

    On setup, stores config and subscribes to the tier1.collect.source hook.
    SafeXCollector is lazy-imported and lazy-instantiated only when
    on_collect is called, because Playwright requires a running browser
    runtime that is not available at setup time.
    """

    def __init__(self) -> None:
        self._event_bus = None
        self._config: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "tier1.x_collector"

    def setup(self, context: Any) -> None:
        """Extract event_bus and config from context.

        SafeXCollector is NOT instantiated here because it requires
        Playwright runtime (running browser). It will be created
        lazily in on_collect.

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
                "tier1.collect.source", self.on_collect, priority=100
            )

        logger.info("XCollectorExtension setup complete")

    def on_collect(
        self, event: str, payload: Dict[str, Any], meta: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Collect tweets from a single search URL.

        Lazy-imports and instantiates SafeXCollector, then runs collection.

        Args:
            event: Event name (tier1.collect.source).
            payload: Must contain:
                - search_url (str, required): X search URL to collect from.
                - max_scrolls (int, optional): Override default max scroll count.
                - group_name (str, optional): Group name for logging.
                - stop_after_empty (int, optional): Stop after N consecutive
                  empty scroll results.
                - profile_path (str, optional): Browser profile path override.
            meta: Event metadata (correlation_id etc.).

        Returns:
            Dict with status, collected_count, and tweets list.
        """
        from collector.x_collector import SafeXCollector

        search_url = payload.get("search_url")
        if not search_url:
            error_result = {
                "status": "error",
                "collected_count": 0,
                "tweets": [],
                "error_message": "search_url is required in payload",
            }
            if self._event_bus is not None:
                self._event_bus.publish("collect.error", error_result, meta)
            return error_result

        profile_path = payload.get(
            "profile_path", self._config.get("profile_path", "./x_profile")
        )
        max_scrolls = payload.get(
            "max_scrolls", self._config.get("max_scrolls", 10)
        )
        group_name = payload.get("group_name", "unknown")
        stop_after_empty = payload.get(
            "stop_after_empty", self._config.get("stop_after_empty", 3)
        )

        collector = SafeXCollector(profile_path=profile_path)

        try:
            result = collector.collect(
                search_url=search_url,
                max_scrolls=max_scrolls,
                group_name=group_name,
                stop_after_empty=stop_after_empty,
            )
        except Exception as exc:
            logger.exception(
                "Collection failed for %s", search_url
            )
            error_result = {
                "status": "error",
                "collected_count": 0,
                "tweets": [],
                "error_message": str(exc),
            }
            if self._event_bus is not None:
                self._event_bus.publish("collect.error", error_result, meta)
            return error_result

        output = {
            "status": result.status,
            "collected_count": result.collected_count,
            "tweets": result.tweets,
        }

        if result.status == "success":
            if self._event_bus is not None:
                self._event_bus.publish(
                    "collect.completed",
                    {
                        "tweets": result.tweets,
                        "collected_count": result.collected_count,
                        "group_name": group_name,
                        "search_url": search_url,
                    },
                    meta,
                )
        else:
            output["error_type"] = result.error_type
            output["error_message"] = result.error_message
            if self._event_bus is not None:
                self._event_bus.publish(
                    "collect.error",
                    {
                        "status": result.status,
                        "error_type": result.error_type,
                        "error_message": result.error_message,
                        "collected_count": result.collected_count,
                        "search_url": search_url,
                    },
                    meta,
                )

        return output

    def on_collect_batch(
        self, event: str, payload: Dict[str, Any], meta: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Collect tweets from multiple search URLs with shared dedup.

        Args:
            event: Event name.
            payload: Must contain:
                - tasks (list[dict], required): List of task dicts, each with
                  the same keys as on_collect payload (search_url, max_scrolls,
                  group_name, stop_after_empty, profile_path).
            meta: Event metadata (correlation_id etc.).

        Returns:
            List of result dicts, one per task.
        """
        from collector.x_collector import SafeXCollector

        tasks = payload.get("tasks", [])
        if not tasks:
            return []

        profile_path = payload.get(
            "profile_path", self._config.get("profile_path", "./x_profile")
        )

        # Shared collected_urls set for dedup across tasks
        shared_urls: set = set()
        collector = SafeXCollector(
            profile_path=profile_path, shared_collected_urls=shared_urls
        )

        results: List[Dict[str, Any]] = []

        for task in tasks:
            search_url = task.get("search_url")
            if not search_url:
                results.append({
                    "status": "error",
                    "collected_count": 0,
                    "tweets": [],
                    "error_message": "search_url is required in task",
                })
                continue

            max_scrolls = task.get(
                "max_scrolls", self._config.get("max_scrolls", 10)
            )
            group_name = task.get("group_name", "unknown")
            stop_after_empty = task.get(
                "stop_after_empty", self._config.get("stop_after_empty", 3)
            )

            try:
                result = collector.collect(
                    search_url=search_url,
                    max_scrolls=max_scrolls,
                    group_name=group_name,
                    stop_after_empty=stop_after_empty,
                )

                task_output = {
                    "status": result.status,
                    "collected_count": result.collected_count,
                    "tweets": result.tweets,
                }

                if result.status == "success":
                    if self._event_bus is not None:
                        self._event_bus.publish(
                            "collect.completed",
                            {
                                "tweets": result.tweets,
                                "collected_count": result.collected_count,
                                "group_name": group_name,
                                "search_url": search_url,
                            },
                            meta,
                        )
                else:
                    task_output["error_type"] = result.error_type
                    task_output["error_message"] = result.error_message
                    if self._event_bus is not None:
                        self._event_bus.publish(
                            "collect.error",
                            {
                                "status": result.status,
                                "error_type": result.error_type,
                                "error_message": result.error_message,
                                "collected_count": result.collected_count,
                                "search_url": search_url,
                            },
                            meta,
                        )

                results.append(task_output)

            except Exception as exc:
                logger.exception(
                    "Batch collection failed for %s", search_url
                )
                error_result = {
                    "status": "error",
                    "collected_count": 0,
                    "tweets": [],
                    "error_message": str(exc),
                }
                if self._event_bus is not None:
                    self._event_bus.publish("collect.error", error_result, meta)
                results.append(error_result)

        return results

    def teardown(self) -> None:
        """Clean up resources."""
        if self._event_bus is not None:
            try:
                self._event_bus.unsubscribe(
                    "tier1.collect.source", self.on_collect
                )
            except ValueError:
                pass
        self._event_bus = None
        self._config = {}
        logger.info("XCollectorExtension teardown complete")
