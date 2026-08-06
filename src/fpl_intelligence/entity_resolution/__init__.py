"""Entity resolution package."""

from .resolver import (
    EntityResolutionReport,
    ManualOverride,
    normalize_name,
    load_manual_overrides,
    resolve_by_name,
)

__all__ = [
    "EntityResolutionReport",
    "ManualOverride",
    "normalize_name",
    "load_manual_overrides",
    "resolve_by_name",
]
