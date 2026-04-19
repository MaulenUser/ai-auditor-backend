"""Ports (abstractions) the application layer depends on.

Concrete adapters live under :mod:`infrastructure`. Any implementation that
satisfies the Protocol is acceptable — this is the Dependency Inversion
Principle: services depend on the abstraction, not on ``requests`` or
:mod:`pathlib`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class BitrixGateway(Protocol):
    """What the application layer needs from Bitrix24."""

    def call(
        self,
        method: str,
        body: dict[str, Any] | None = None,
        label: str | None = None,
    ) -> dict[str, Any]: ...

    def list_all(
        self,
        method: str,
        select: list[str],
        filter: dict[str, Any] | None = None,  # noqa: A002
        order: dict[str, Any] | None = None,
        context: str = "",
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...


class JsonSink(Protocol):
    """A destination that can persist a JSON-serialisable document."""

    def write(self, path: str | Path, data: Any) -> None: ...


class FileDownloader(Protocol):
    """Downloads a remote URL to a local file."""

    def download(self, url: str, destination: Path) -> None: ...
