"""RunContext: shared state passed to extensions during a run."""

import logging
import uuid
from typing import Any, Dict


class RunContext:
    """Holds shared state for a single execution run.

    Passed to extensions so they can access the event bus, registry,
    config, and logging without global state.

    Attributes:
        event_bus: EventBus instance for inter-extension communication.
        registry: ExtensionRegistry instance.
        config: Merged configuration dict.
        run_id: Unique identifier for this run.
        logger: Logger instance.
    """

    def __init__(
        self,
        event_bus: Any,
        registry: Any,
        config: Dict[str, Any],
        run_id: str = "",
        logger: logging.Logger = None,
    ):
        self.event_bus = event_bus
        self.registry = registry
        self.config = config
        self.run_id = run_id or str(uuid.uuid4())
        self.logger = logger or logging.getLogger("influx")

    def get_extension_config(self, ext_name: str) -> Dict[str, Any]:
        """Get configuration for a specific extension.

        Looks up config["extensions"]["ext_configs"][ext_name].

        Args:
            ext_name: Extension name.

        Returns:
            Extension-specific config dict, or empty dict if not found.
        """
        ext_configs = self.config.get("extensions", {}).get("ext_configs", {})
        return dict(ext_configs.get(ext_name, {}))
