"""UTF-8 JSON writer with consistent formatting.

Exposed both as a class (for DI into services) and as module-level functions
(for ergonomics and backwards compatibility).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def ensure_dir(path: str | Path) -> Path:
    """Create *path* (and parents) if missing; return it as a :class:`Path`."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_json_file(path: str | Path, data: Any, indent: int = 2) -> None:
    """Write *data* as UTF-8 JSON to *path*, creating parent dirs as needed."""
    FileSystemJsonWriter(indent=indent).write(path, data)


class FileSystemJsonWriter:
    """Writes JSON to the local filesystem.

    Exists as a class so services can depend on an abstraction (the
    ``JsonSink`` Protocol in :mod:`application.ports`) and be tested with
    in-memory fakes.
    """

    def __init__(self, indent: int = 2) -> None:
        self._indent = indent

    def write(self, path: str | Path, data: Any) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(destination, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=self._indent, default=str)
