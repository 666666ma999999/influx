"""EventBus - Pub/Sub infrastructure for extension inter-communication.

Provides a thread-safe, priority-based event bus with dead-letter capture
and correlation IDs for observability.
"""

import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class _Subscription:
    """Internal representation of an event handler subscription."""

    handler: Callable
    priority: int


@dataclass
class DeadLetter:
    """Record of a failed handler invocation.

    Attributes:
        event: The event name that was published.
        handler_name: Qualified name of the handler that failed.
        payload: The event payload that was passed.
        exception: The exception that was raised.
        correlation_id: The correlation ID of the publish call.
    """

    event: str
    handler_name: str
    payload: Dict[str, Any]
    exception: Exception
    correlation_id: str


class EventBus:
    """Thread-safe, priority-based pub/sub event bus.

    Handlers are invoked in priority order (lower value = earlier execution).
    Exceptions in handlers are captured as dead letters and do not prevent
    subsequent handlers from executing.

    Example:
        >>> bus = EventBus()
        >>> bus.subscribe("tweet.classified", my_handler, priority=10)
        >>> results = bus.publish("tweet.classified", {"text": "hello"})
    """

    def __init__(self) -> None:
        self._subscriptions: Dict[str, List[_Subscription]] = {}
        self._lock = threading.Lock()
        self._dead_letters: List[DeadLetter] = []

    def subscribe(
        self,
        event: str,
        handler: Callable,
        priority: int = 100,
    ) -> None:
        """Register a handler for an event.

        Args:
            event: The event name to subscribe to.
            handler: Callable to invoke when the event is published.
                Signature: handler(event, payload, meta) -> Any
            priority: Execution priority. Lower values run first.
                Defaults to 100.
        """
        with self._lock:
            if event not in self._subscriptions:
                self._subscriptions[event] = []
            self._subscriptions[event].append(
                _Subscription(handler=handler, priority=priority)
            )
            self._subscriptions[event].sort(key=lambda s: s.priority)

    def unsubscribe(self, event: str, handler: Callable) -> None:
        """Remove a handler from an event.

        Args:
            event: The event name to unsubscribe from.
            handler: The handler callable to remove.

        Raises:
            ValueError: If the handler is not subscribed to the event.
        """
        with self._lock:
            subs = self._subscriptions.get(event, [])
            original_len = len(subs)
            self._subscriptions[event] = [
                s for s in subs if s.handler is not handler
            ]
            if len(self._subscriptions[event]) == original_len:
                raise ValueError(
                    f"Handler {handler!r} is not subscribed to event '{event}'"
                )

    def publish(
        self,
        event: str,
        payload: Dict[str, Any],
        meta: Optional[Dict[str, Any]] = None,
    ) -> List[Any]:
        """Publish an event to all subscribed handlers.

        Handlers are called in priority order. If a handler raises an
        exception, it is captured as a dead letter and execution continues
        with the remaining handlers.

        Args:
            event: The event name to publish.
            payload: Event data passed to each handler.
            meta: Optional metadata dict. A 'correlation_id' is always
                injected automatically.

        Returns:
            List of return values from handlers that executed successfully.
        """
        correlation_id = uuid.uuid4().hex
        if meta is None:
            meta = {}
        meta["correlation_id"] = correlation_id

        with self._lock:
            subs = list(self._subscriptions.get(event, []))

        results: List[Any] = []
        for sub in subs:
            try:
                result = sub.handler(event, payload, meta)
                results.append(result)
            except Exception as exc:
                handler_name = getattr(
                    sub.handler, "__qualname__", repr(sub.handler)
                )
                dead = DeadLetter(
                    event=event,
                    handler_name=handler_name,
                    payload=payload,
                    exception=exc,
                    correlation_id=correlation_id,
                )
                with self._lock:
                    self._dead_letters.append(dead)
                logger.error(
                    "EventBus dead-letter: event=%s handler=%s correlation_id=%s error=%s",
                    event,
                    handler_name,
                    correlation_id,
                    exc,
                )

        return results

    @property
    def dead_letters(self) -> List[DeadLetter]:
        """Return a copy of all captured dead letters."""
        with self._lock:
            return list(self._dead_letters)

    def clear_dead_letters(self) -> None:
        """Clear all captured dead letters."""
        with self._lock:
            self._dead_letters.clear()
