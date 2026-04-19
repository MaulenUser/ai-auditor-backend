"""BitrixClient: high-level API surface composed of transport + retry policy.

This client focuses on exactly one concern - issuing a single Bitrix REST call
with retry semantics. Pagination lives in :mod:`.paginator` so ``list_all`` is
no longer a method on the client but a dedicated iterable. The client itself
still exposes ``list_all`` for convenience and backwards compatibility.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from ...domain.exceptions import BitrixError
from .paginator import ListPaginator
from .retry_policy import RetryPolicy
from .transport import ConnectionFailed, HttpStatusError, HttpTransport, RequestsTransport
from .webhook_url import WebhookUrl

logger = logging.getLogger(__name__)


class BitrixClient:
    """Orchestrates a Bitrix REST call: build URL -> POST -> retry -> return JSON.

    The client does not know anything about Bitrix entity shapes. It is a
    generic REST invoker. Service classes (CrmExportService etc.) know which
    methods to call and how to interpret the payload.
    """

    def __init__(
        self,
        webhook_url: WebhookUrl | str,
        *,
        transport: HttpTransport | None = None,
        retry_policy: RetryPolicy | None = None,
        page_delay: float = 0.0,
        max_attempts: int | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._url = webhook_url if isinstance(webhook_url, WebhookUrl) else WebhookUrl.parse(webhook_url)
        self._transport: HttpTransport = transport or RequestsTransport(session=session)
        policy = retry_policy or RetryPolicy()
        if max_attempts is not None:
            policy = RetryPolicy(
                max_attempts=max_attempts,
                backoff=policy.backoff,
                classifier=policy.classifier,
            )
        self._policy = policy
        self._page_delay = page_delay

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        """Backwards-compatible accessor; returns the normalised webhook URL as a string."""
        return str(self._url)

    @property
    def max_attempts(self) -> int:
        return self._policy.max_attempts

    @property
    def page_delay(self) -> float:
        return self._page_delay

    def call(
        self,
        method: str,
        body: dict[str, Any] | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """POST to *method* and return the parsed JSON body.

        Retries transient failures according to the configured :class:`RetryPolicy`.
        On exhaustion or on a non-transient error, raises :class:`BitrixError`.
        """
        url = self._url.method_url(method)
        effective_label = label or method
        payload = body or {}

        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                return self._transport.post_json(url, payload)
            except HttpStatusError as exc:
                if self._should_retry_status(exc.status_code, attempt):
                    self._sleep_before_retry(attempt, effective_label, f"HTTP {exc.status_code}")
                    continue
                raise BitrixError(
                    f"Bitrix request '{effective_label}' failed",
                    status_code=exc.status_code,
                    response_body=exc.body,
                ) from exc
            except ConnectionFailed as exc:
                if self._should_retry_connection(str(exc), attempt):
                    self._sleep_before_retry(attempt, effective_label, f"connection error: {exc}")
                    continue
                raise BitrixError(
                    f"Bitrix request '{effective_label}' failed: {exc}"
                ) from exc

        raise BitrixError(
            f"Bitrix request '{effective_label}' exhausted {self._policy.max_attempts} attempts"
        )

    def list_all(
        self,
        method: str,
        select: list[str],
        filter: dict[str, Any] | None = None,  # noqa: A002 - PS-parity name
        order: dict[str, Any] | None = None,
        context: str = "",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch list-endpoint pages through :class:`ListPaginator`."""
        paginator = ListPaginator(
            client=self,
            method=method,
            select=select,
            filter=filter,
            order=order,
            context=context,
            page_delay=self._page_delay,
            limit=limit,
        )
        return list(paginator)

    # ------------------------------------------------------------------
    # Internal - retry coordination
    # ------------------------------------------------------------------

    def _should_retry_status(self, status_code: int | None, attempt: int) -> bool:
        return (
            self._policy.classifier.is_transient_status(status_code)
            and self._policy.should_retry(attempt)
        )

    def _should_retry_connection(self, message: str, attempt: int) -> bool:
        return (
            self._policy.classifier.is_transient_message(message)
            and self._policy.should_retry(attempt)
        )

    def _sleep_before_retry(self, attempt: int, label: str, reason: str) -> None:
        delay = self._policy.backoff.delay_for(attempt)
        logger.warning(
            "Retrying '%s' after %s on attempt %d/%d (delay=%.1fs)",
            label,
            reason,
            attempt,
            self._policy.max_attempts,
            delay,
        )
        time.sleep(delay)
