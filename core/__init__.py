"""Core infrastructure for the influx extension architecture."""

from core.event_bus import DeadLetter, EventBus
from core.registry import Extension, ExtensionManifest, ExtensionRegistry

__all__ = [
    "EventBus",
    "DeadLetter",
    "Extension",
    "ExtensionManifest",
    "ExtensionRegistry",
]
