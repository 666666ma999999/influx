"""Config loader with YAML support and environment variable overrides."""

import os
from copy import deepcopy
from typing import Any, Dict, Optional

from core.errors import ConfigError


class ConfigLoader:
    """Loads and merges configuration from YAML files and environment variables."""

    @staticmethod
    def load_yaml(path: str) -> Dict[str, Any]:
        """Load a YAML file and return its contents as a dict.

        Args:
            path: Path to the YAML file.

        Returns:
            Parsed YAML content. Empty dict if file does not exist.

        Raises:
            ConfigError: If PyYAML is not installed or the file is malformed.
        """
        if not os.path.isfile(path):
            return {}

        try:
            import yaml
        except ImportError:
            raise ConfigError(
                "PyYAML is required for YAML config files. "
                "Install it with: pip install pyyaml"
            )

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
        except yaml.YAMLError as e:
            raise ConfigError(f"Failed to parse YAML file {path}: {e}")

    @staticmethod
    def merge_configs(*configs: Dict[str, Any]) -> Dict[str, Any]:
        """Deep-merge multiple config dicts. Later values override earlier ones.

        Args:
            *configs: Config dicts to merge in order.

        Returns:
            Merged config dict.
        """
        result: Dict[str, Any] = {}
        for config in configs:
            if not config:
                continue
            result = _deep_merge(result, deepcopy(config))
        return result

    @staticmethod
    def apply_env_overrides(
        config: Dict[str, Any], prefix: str = "INFLUX"
    ) -> Dict[str, Any]:
        """Override config values from environment variables.

        Environment variables are mapped to nested keys using double underscore
        as separator. For example, INFLUX__APP__LOG_LEVEL maps to
        config["app"]["log_level"].

        Args:
            config: Base config dict.
            prefix: Environment variable prefix.

        Returns:
            Config dict with environment overrides applied.
        """
        result = deepcopy(config)
        env_prefix = f"{prefix}__"

        for key, value in os.environ.items():
            if not key.startswith(env_prefix):
                continue

            parts = key[len(env_prefix):].lower().split("__")
            _set_nested(result, parts, _parse_env_value(value))

        return result


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge override into base."""
    merged = dict(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _set_nested(config: Dict[str, Any], parts: list, value: Any) -> None:
    """Set a value in a nested dict using a list of keys."""
    for part in parts[:-1]:
        if part not in config or not isinstance(config[part], dict):
            config[part] = {}
        config = config[part]
    config[parts[-1]] = value


def _parse_env_value(value: str) -> Any:
    """Parse an environment variable value to its Python type."""
    if value.lower() in ("true", "yes"):
        return True
    if value.lower() in ("false", "no"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value
