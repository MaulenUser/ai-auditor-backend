"""HTTP file downloader that records trace timings for /audit/run."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from ..audit_trace import AuditTraceRecorder


class RequestsFileDownloader:
    """Downloads a file from a URL to a local path using streaming GET."""

    def __init__(
        self,
        timeout_seconds: int = 120,
        *,
        trace: AuditTraceRecorder | None = None,
        trace_name: str = "file.download",
    ) -> None:
        self._timeout = timeout_seconds
        self._trace = trace
        self._trace_name = trace_name

    def download(self, url: str, destination: Path) -> None:
        started_at = datetime.now(tz=timezone.utc)
        try:
            response = requests.get(url, timeout=self._timeout, stream=True)
            response.raise_for_status()
            with open(destination, "wb") as fh:
                for chunk in response.iter_content(chunk_size=8192):
                    fh.write(chunk)
        except Exception as exc:  # noqa: BLE001
            self._record_trace(
                started_at=started_at,
                status="error",
                url=url,
                destination=destination,
                error=exc,
            )
            raise
        self._record_trace(
            started_at=started_at,
            status="ok",
            url=url,
            destination=destination,
        )

    def _record_trace(
        self,
        *,
        started_at: datetime,
        status: str,
        url: str,
        destination: Path,
        error: BaseException | None = None,
    ) -> None:
        if not self._trace:
            return
        parsed = urlparse(url)
        self._trace.record_operation(
            "file_download",
            self._trace_name,
            started_at=started_at,
            finished_at=datetime.now(tz=timezone.utc),
            status=status,
            details={
                "url_host": parsed.netloc,
                "url_path": parsed.path,
                "destination": destination.as_posix(),
                "file_name": destination.name,
                "size_bytes": destination.stat().st_size if destination.exists() else None,
            },
            error=error,
        )
