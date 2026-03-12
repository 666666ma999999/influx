"""InfluxApp: application bootstrap and pipeline execution.

Creates the EventBus, ExtensionRegistry, and RunContext, then discovers,
resolves, and loads extensions from the configured extensions directory.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import load_config
from core.context import RunContext
from core.errors import ConfigError, ExtensionError
from core.event_bus import EventBus
from core.registry import ExtensionManifest, ExtensionRegistry

logger = logging.getLogger(__name__)


class InfluxApp:
    """Main application class that bootstraps the extension architecture.

    Attributes:
        config_path: Path to the configs directory.
        config: Merged configuration dict.
        event_bus: EventBus instance.
        registry: ExtensionRegistry instance.
    """

    def __init__(self, config_path: str = "configs") -> None:
        self.config_path = config_path
        self.config: Dict[str, Any] = {}
        self.event_bus: Optional[EventBus] = None
        self.registry: Optional[ExtensionRegistry] = None
        self._context: Optional[RunContext] = None
        self._load_order: List[str] = []

    def setup(self) -> None:
        """Load config, create EventBus and Registry, discover and load extensions."""
        profile = os.environ.get("INFLUX_PROFILE")
        self.config = load_config(base_path=self.config_path, profile=profile)

        log_level = self.config.get("app", {}).get("log_level", "INFO")
        logging.basicConfig(level=getattr(logging, log_level, logging.INFO))

        self.event_bus = EventBus()
        self.registry = ExtensionRegistry()

        ext_path = self.config.get("extensions", {}).get("path", "extensions")
        enabled = self._load_enabled_list()

        manifest_paths = self._discover_manifests(ext_path)
        for mpath in manifest_paths:
            manifest = self._load_manifest(mpath)
            if enabled is not None and manifest.name not in enabled:
                del self.registry.manifests[manifest.name]
                logger.info("Extension disabled by config: %s", manifest.name)

        self._load_order = self.registry.resolve()
        self.registry.load(event_bus=self.event_bus, config=self.config)

        self._context = RunContext(
            event_bus=self.event_bus,
            registry=self.registry,
            config=self.config,
        )

        logger.info(
            "InfluxApp setup complete. %d extensions loaded.",
            len(self.registry.extensions),
        )

    def run_pipeline(self, pipeline_name: str, **kwargs: Any) -> dict:
        """Run a named pipeline by publishing its hook events.

        Args:
            pipeline_name: Pipeline identifier (e.g. "classify", "collect").
            **kwargs: Additional payload data passed to the hook.

        Returns:
            Dict with pipeline results.
        """
        if self.event_bus is None:
            raise ExtensionError("InfluxApp.setup() has not been called")

        payload: Dict[str, Any] = {"pipeline": pipeline_name, **kwargs}

        pre_results = self.event_bus.publish(f"{pipeline_name}.pre", payload)
        main_results = self.event_bus.publish(pipeline_name, payload)
        post_results = self.event_bus.publish(f"{pipeline_name}.post", payload)

        return {
            "pipeline": pipeline_name,
            "pre": pre_results,
            "main": main_results,
            "post": post_results,
        }

    def teardown(self) -> None:
        """Teardown all loaded extensions in reverse load order."""
        if self.registry is None:
            return

        for name in reversed(self._load_order):
            ext = self.registry.get_extension(name)
            if ext is None:
                continue
            try:
                ext.teardown()
                logger.info("Extension teardown: %s", name)
            except Exception:
                logger.exception("Extension teardown failed: %s", name)

    @property
    def context(self) -> RunContext:
        """Return the current RunContext."""
        if self._context is None:
            raise ExtensionError("InfluxApp.setup() has not been called")
        return self._context

    def _load_enabled_list(self) -> Optional[List[str]]:
        """Load the list of enabled extensions from extensions.enabled.yaml.

        Returns:
            List of enabled extension names, or None if the file does not exist
            (meaning all discovered extensions are enabled).
        """
        enabled_path = os.path.join(self.config_path, "extensions.enabled.yaml")
        if not os.path.isfile(enabled_path):
            return None

        try:
            import yaml
        except ImportError:
            logger.warning("PyYAML not installed; skipping extensions.enabled.yaml")
            return None

        with open(enabled_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if isinstance(data, dict):
            return data.get("enabled", [])
        if isinstance(data, list):
            return data
        return None

    def _load_manifest(self, yaml_path: str) -> ExtensionManifest:
        """Load a manifest from either flat (manifest.yaml) or structured (extension.yaml) format.

        The structured format uses nested keys like metadata.name, runtime.entrypoint.
        This normalizes them into the flat format expected by ExtensionRegistry.

        Args:
            yaml_path: Path to the manifest YAML file.

        Returns:
            Loaded ExtensionManifest.
        """
        try:
            import yaml
        except ImportError:
            raise ConfigError("PyYAML is required for extension manifests.")

        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        # If the file has a top-level "name", it's the flat format.
        # Use the registry's standard loader.
        if "name" in data:
            return self.registry.load_manifest(yaml_path)

        # Structured format (extension.yaml): normalize to flat.
        metadata = data.get("metadata", {})
        runtime = data.get("runtime", {})
        name = metadata.get("name")
        if not name:
            raise ConfigError(f"Extension manifest missing metadata.name: {yaml_path}")

        flat = {
            "name": name,
            "version": metadata.get("version", "0.1.0"),
            "tier": metadata.get("tier", "tier2"),
            "description": metadata.get("description", ""),
            "entrypoint": runtime.get("entrypoint", ""),
            "dependencies": data.get("dependencies", {"requires": [], "optional": []}),
            "hooks": data.get("hooks", {"subscribes": [], "emits": []}),
            "config": data.get("config", {"schema": "", "defaults": {}}),
            "contracts": data.get("contracts", {"consumes": [], "produces": []}),
        }

        manifest = ExtensionManifest(**flat)
        self.registry.manifests[manifest.name] = manifest
        logger.info("Manifest registered: %s (%s)", manifest.name, manifest.version)
        return manifest

    def _discover_manifests(self, ext_path: str) -> List[str]:
        """Recursively discover extension manifest files.

        Searches for both manifest.yaml and extension.yaml in the
        extensions directory tree.

        Args:
            ext_path: Relative or absolute path to extensions directory.

        Returns:
            List of manifest file paths.
        """
        base = Path(self.registry.base_path)
        ext_dir = Path(ext_path) if os.path.isabs(ext_path) else base / ext_path

        if not ext_dir.is_dir():
            logger.warning("Extensions directory not found: %s", ext_dir)
            return []

        found: List[str] = []
        for manifest_name in ("manifest.yaml", "extension.yaml"):
            for path in sorted(ext_dir.rglob(manifest_name)):
                found.append(str(path))
                logger.debug("Manifest discovered: %s", path)

        return found


def create_app(config_path: str = "configs") -> InfluxApp:
    """Factory function to create and setup an InfluxApp instance.

    Args:
        config_path: Path to the configs directory.

    Returns:
        Fully initialized InfluxApp.
    """
    app = InfluxApp(config_path=config_path)
    app.setup()
    return app
