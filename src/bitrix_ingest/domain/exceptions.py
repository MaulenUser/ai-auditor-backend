"""Domain exceptions.

These are raised by the domain/application layers and are decoupled from any
specific transport (requests, aiohttp, etc.). Infrastructure adapters map
their native errors into these types so higher layers stay transport-agnostic.
"""
from __future__ import annotations


class DomainError(Exception):
    """Base class for all errors raised by the ingestion layer."""


class BitrixError(DomainError):
    """A Bitrix24 API call failed in a way that cannot be recovered from.

    Carries transport context (HTTP status code, response body) when available
    so operators can diagnose issues without inspecting lower layers.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
