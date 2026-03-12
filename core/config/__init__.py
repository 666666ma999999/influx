"""Config package: loads and merges configuration from multiple sources.

Merge order: defaults.yaml -> app.yaml -> profile override -> env vars (INFLUX__*)
"""

import os
from typing import Any, Dict, Optional

from core.config.loader import ConfigLoader


def load_config(
    base_path: str = "configs",
    profile: Optional[str] = None,
) -> Dict[str, Any]:
    """Load merged configuration from all sources.

    Args:
        base_path: Directory containing config YAML files.
        profile: Optional profile name (loads configs/{profile}.yaml).

    Returns:
        Fully merged config dict.
    """
    loader = ConfigLoader()

    defaults_path = os.path.join(
        os.path.dirname(__file__), "defaults.yaml"
    )
    defaults = loader.load_yaml(defaults_path)

    app_path = os.path.join(base_path, "app.yaml")
    app_config = loader.load_yaml(app_path)

    profile_config: Dict[str, Any] = {}
    if profile:
        profile_path = os.path.join(base_path, f"{profile}.yaml")
        profile_config = loader.load_yaml(profile_path)

    merged = loader.merge_configs(defaults, app_config, profile_config)
    merged = loader.apply_env_overrides(merged, prefix="INFLUX")

    return merged
