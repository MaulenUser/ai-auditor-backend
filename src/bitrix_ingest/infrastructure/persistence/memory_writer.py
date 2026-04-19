"""In-memory JsonSink — collects writes instead of flushing to disk."""
from __future__ import annotations

from pathlib import Path
from typing import Any


class InMemoryJsonSink:
    """JsonSink that keeps all written data in a dict.

    Keys are POSIX-style relative paths (e.g. ``"deals.json"``,
    ``"conversations/deal_1.json"``).  Useful for tests and the REST API.
    """

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    def write(self, path: str | Path, data: Any) -> None:
        self._store[Path(path).as_posix()] = data

    @property
    def data(self) -> dict[str, Any]:
        return dict(self._store)


class TeeJsonSink:
    """JsonSink that forwards each write to two sinks (fan-out)."""

    def __init__(self, primary: Any, secondary: Any) -> None:
        self._primary = primary
        self._secondary = secondary

    def write(self, path: str | Path, data: Any) -> None:
        self._primary.write(path, data)
        self._secondary.write(path, data)
