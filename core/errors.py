"""Core exception classes for the influx extension architecture."""


class InfluxError(Exception):
    """Base exception for all influx errors."""


class ExtensionError(InfluxError):
    """Raised when an extension fails to load or execute."""


class ManifestError(InfluxError):
    """Raised when an extension manifest is invalid or missing required fields."""


class HookError(InfluxError):
    """Raised when a hook point execution fails."""


class ConfigError(InfluxError):
    """Raised when configuration loading or merging fails."""
