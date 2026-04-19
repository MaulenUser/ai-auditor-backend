"""HTTP transport abstraction and its default ``requests``-based adapter.

The ``HttpTransport`` Protocol lets the BitrixClient stay unaware of how the
POST actually happens — making it trivial to mock in tests or swap for an
``aiohttp``/``httpx`` adapter without touching higher layers.
"""
from __future__ import annotations

from typing import Any, Protocol

import requests


class TransportError(Exception):
    """Raised by a transport when the underlying I/O fails."""


class HttpStatusError(TransportError):
    """Raised for non-2xx responses. Carries the status code for retry logic."""

    def __init__(
        self, message: str, status_code: int | None, body: str | None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ConnectionFailed(TransportError):
    """Raised when the request could not reach the server or timed out."""


class HttpTransport(Protocol):
    """Fire-and-forget JSON POST. Retry logic lives one layer up."""

    def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class RequestsTransport:
    """Thin adapter around ``requests.Session`` that converts native errors
    into the transport-level exceptions defined above."""

    _JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout_seconds: int = 60,
    ) -> None:
        self._session = session or requests.Session()
        self._timeout = timeout_seconds

    def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._session.post(
                url,
                json=payload,
                headers=self._JSON_HEADERS,
                timeout=self._timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            body = exc.response.text if exc.response is not None else None
            raise HttpStatusError(str(exc), status_code=status_code, body=body) from exc
        except requests.RequestException as exc:
            raise ConnectionFailed(str(exc)) from exc
