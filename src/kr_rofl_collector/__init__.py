"""Compatibility imports for the former KR-only package name."""

from __future__ import annotations

import importlib
import sys

from global_rofl_collector import __version__ as __version__

_MODULES = (
    "cli",
    "config",
    "db",
    "discovery",
    "errors",
    "locking",
    "maintenance",
    "manifest",
    "models",
    "platforms",
    "replay",
    "riot",
    "service",
)

for _name in _MODULES:
    _module = importlib.import_module(f"global_rofl_collector.{_name}")
    sys.modules[f"{__name__}.{_name}"] = _module
    globals()[_name] = _module
