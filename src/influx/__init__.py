"""Influx package metadata."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("influx")
except PackageNotFoundError:  # pragma: no cover - source tree fallback
    __version__ = "0.0.0"
