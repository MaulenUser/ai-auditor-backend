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
from ..audit_trace import AuditTraceRecorder
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
        call_delay: float = 0.0,
        max_attempts: int | None = None,
        session: requests.Session | None = None,
        trace: AuditTraceRecorder | None = None,
        trace_name: str = "bitrix",
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
        self._call_delay = call_delay
        self._trace = trace
        self._trace_name = trace_name

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

    @property
    def call_delay(self) -> float:
        return self._call_delay

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
        started_at = self._trace_now()
        retry_reasons: list[str] = []
        if self._call_delay > 0:
            time.sleep(self._call_delay)

        url = self._url.method_url(method)
        effective_label = label or method
        payload = body or {}

        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                response = self._transport.post_json(url, payload)
                self._record_trace(
                    event_type="bitrix_call",
                    name=method,
                    started_at=started_at,
                    status="ok",
                    details={
                        **self._payload_trace_details(payload),
                        "label": effective_label,
                        "trace_name": self._trace_name,
                        "attempts": attempt,
                        "retry_reasons": retry_reasons,
                        "result_count": self._result_count(response),
                        "has_next": response.get("next") is not None,
                    },
                )
                return response
            except HttpStatusError as exc:
                if self._should_retry_status(exc.status_code, attempt):
                    retry_reasons.append(f"HTTP {exc.status_code}")
                    self._sleep_before_retry(attempt, effective_label, f"HTTP {exc.status_code}")
                    continue
                self._record_trace(
                    event_type="bitrix_call",
                    name=method,
                    started_at=started_at,
                    status="error",
                    details={
                        **self._payload_trace_details(payload),
                        "label": effective_label,
                        "trace_name": self._trace_name,
                        "attempts": attempt,
                        "retry_reasons": retry_reasons,
                        "status_code": exc.status_code,
                    },
                    error=exc,
                )
                raise BitrixError(
                    f"Bitrix request '{effective_label}' failed",
                    status_code=exc.status_code,
                    response_body=exc.body,
                ) from exc
            except ConnectionFailed as exc:
                if self._should_retry_connection(str(exc), attempt):
                    retry_reasons.append(str(exc))
                    self._sleep_before_retry(attempt, effective_label, f"connection error: {exc}")
                    continue
                self._record_trace(
                    event_type="bitrix_call",
                    name=method,
                    started_at=started_at,
                    status="error",
                    details={
                        **self._payload_trace_details(payload),
                        "label": effective_label,
                        "trace_name": self._trace_name,
                        "attempts": attempt,
                        "retry_reasons": retry_reasons,
                    },
                    error=exc,
                )
                raise BitrixError(
                    f"Bitrix request '{effective_label}' failed: {exc}"
                ) from exc

        self._record_trace(
            event_type="bitrix_call",
            name=method,
            started_at=started_at,
            status="error",
            details={
                **self._payload_trace_details(payload),
                "label": effective_label,
                "trace_name": self._trace_name,
                "attempts": self._policy.max_attempts,
                "retry_reasons": retry_reasons,
            },
        )
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
        started_at = self._trace_now()
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
        try:
            items = list(paginator)
        except Exception as exc:  # noqa: BLE001
            self._record_trace(
                event_type="bitrix_list_all",
                name=method,
                started_at=started_at,
                status="error",
                details={
                    "trace_name": self._trace_name,
                    "context": context,
                    "limit": limit,
                    "select_count": len(select),
                    "filter_keys": sorted((filter or {}).keys()),
                    "order_keys": sorted((order or {}).keys()),
                },
                error=exc,
            )
            raise
        self._record_trace(
            event_type="bitrix_list_all",
            name=method,
            started_at=started_at,
            status="ok",
            details={
                "trace_name": self._trace_name,
                "context": context,
                "limit": limit,
                "select_count": len(select),
                "filter_keys": sorted((filter or {}).keys()),
                "order_keys": sorted((order or {}).keys()),
                "row_count": len(items),
            },
        )
        return items

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

    @staticmethod
    def _trace_now():
        import datetime as _datetime

        return _datetime.datetime.now(tz=_datetime.timezone.utc)

    @staticmethod
    def _payload_trace_details(payload: dict[str, Any]) -> dict[str, Any]:
        filter_payload = payload.get("filter")
        order_payload = payload.get("order")
        details: dict[str, Any] = {
            "payload_keys": sorted(payload.keys()),
        }
        if isinstance(filter_payload, dict):
            details["filter_keys"] = sorted(str(k) for k in filter_payload.keys())
        if isinstance(order_payload, dict):
            details["order_keys"] = sorted(str(k) for k in order_payload.keys())
        if "start" in payload:
            details["cursor_start"] = payload.get("start")
        if isinstance(payload.get("select"), list):
            details["select_count"] = len(payload["select"])
        return details

    @staticmethod
    def _result_count(response: dict[str, Any]) -> int:
        result = response.get("result")
        if result is None:
            return 0
        if isinstance(result, list):
            return len(result)
        return 1

    def _record_trace(
        self,
        *,
        event_type: str,
        name: str,
        started_at,
        status: str,
        details: dict[str, Any],
        error: BaseException | None = None,
    ) -> None:
        if not self._trace:
            return
        self._trace.record_operation(
            event_type,
            name,
            started_at=started_at,
            finished_at=self._trace_now(),
            status=status,
            details=details,
            error=error,
        )
