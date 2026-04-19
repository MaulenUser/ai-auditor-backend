"""HTTP file downloader — streams a remote URL to a local path."""
from __future__ import annotations

from pathlib import Path

import requests


class RequestsFileDownloader:
    """Downloads a file from a URL to a local path using streaming GET."""

    def __init__(self, timeout_seconds: int = 120) -> None:
        self._timeout = timeout_seconds

    def download(self, url: str, destination: Path) -> None:
        response = requests.get(url, timeout=self._timeout, stream=True)
        response.raise_for_status()
        with open(destination, "wb") as fh:
            for chunk in response.iter_content(chunk_size=8192):
                fh.write(chunk)
