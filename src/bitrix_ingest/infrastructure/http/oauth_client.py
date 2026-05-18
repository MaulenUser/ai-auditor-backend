"""Bitrix24 REST client backed by OAuth access/refresh tokens."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any
from urllib.parse import urlparse

import requests

from ...domain.bitrix_oauth import BitrixOAuthToken
from ...domain.exceptions import BitrixError
from ..audit_trace import AuditTraceRecorder
from ..database import BitrixOAuthRepository
from .client import BitrixClient
from .paginator import ListPaginator
from .retry_policy import RetryPolicy
from .transport import HttpTransport, RequestsTransport

logger = logging.getLogger(__name__)

DEFAULT_BITRIX_OAUTH_TOKEN_ENDPOINT = "https://oauth.bitrix.info/oauth/token/"
DEFAULT_REFRESH_SKEW_SECONDS = 120
DEFAULT_TIMEOUT_SECONDS = 60
_TOKEN_ERROR_MARKERS = ("expired_token", "invalid_token")


class BitrixOAuthClient:
    """Calls Bitrix REST through OAuth and refreshes access tokens as needed."""

    def __init__(
        self,
        tenant_id: str,
        repository: BitrixOAuthRepository,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_endpoint: str = DEFAULT_BITRIX_OAUTH_TOKEN_ENDPOINT,
        session: requests.Session | None = None,
        transport: HttpTransport | None = None,
        retry_policy: RetryPolicy | None = None,
        page_delay: float = 0.0,
        call_delay: float = 0.0,
        max_attempts: int | None = None,
        refresh_skew_seconds: int = DEFAULT_REFRESH_SKEW_SECONDS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        trace: AuditTraceRecorder | None = None,
        trace_name: str = "bitrix.oauth",
    ) -> None:
        self._tenant_id = tenant_id
        self._repository = repository
        self._client_id = (client_id or os.environ.get("BITRIX_OAUTH_CLIENT_ID", "")).strip()
        self._client_secret = (
            client_secret or os.environ.get("BITRIX_OAUTH_CLIENT_SECRET", "")
        ).strip()
        self._token_endpoint = token_endpoint
        self._session = session or requests.Session()
        self._transport = transport or RequestsTransport(session=self._session)
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
        self._refresh_skew_seconds = refresh_skew_seconds
        self._timeout_seconds = timeout_seconds
        self._trace = trace
        self._trace_name = trace_name

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

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
        token = self._current_token()
        if self._needs_refresh(token):
            token = self.refresh_access_token(token)

        try:
            response = self._call_with_token(token, method, body, label)
        except BitrixError as exc:
            if not self._is_expired_token_error(exc):
                raise
            token = self.refresh_access_token(token)
            return self._call_with_token(token, method, body, label)

        if self._is_expired_token_response(response):
            token = self.refresh_access_token(token)
            return self._call_with_token(token, method, body, label)
        return response

    def list_all(
        self,
        method: str,
        select: list[str],
        filter: dict[str, Any] | None = None,  # noqa: A002 - Bitrix REST name
        order: dict[str, Any] | None = None,
        context: str = "",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        paginator = ListPaginator(
            client=self,  # type: ignore[arg-type]
            method=method,
            select=select,
            filter=filter,
            order=order,
            context=context,
            page_delay=self._page_delay,
            limit=limit,
        )
        return list(paginator)

    def refresh_access_token(
        self,
        token: BitrixOAuthToken | None = None,
    ) -> BitrixOAuthToken:
        current = token or self._current_token()
        self._require_refresh_credentials()

        params = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": current.refresh_token,
        }
        response = self._request_refresh(params)
        refreshed = self._token_from_refresh_response(current, response)
        self._repository.save(refreshed)
        return refreshed

    def _call_with_token(
        self,
        token: BitrixOAuthToken,
        method: str,
        body: dict[str, Any] | None,
        label: str | None,
    ) -> dict[str, Any]:
        payload = dict(body or {})
        payload["auth"] = token.access_token
        client = BitrixClient(
            token.client_endpoint,
            transport=self._transport,
            retry_policy=self._policy,
            page_delay=self._page_delay,
            call_delay=self._call_delay,
            trace=self._trace,
            trace_name=self._trace_name,
        )
        return client.call(method, body=payload, label=label)

    def _request_refresh(self, params: dict[str, str]) -> dict[str, Any]:
        retry_reasons: list[str] = []
        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                response = self._session.get(
                    self._token_endpoint,
                    params=params,
                    timeout=self._timeout_seconds,
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise BitrixError("Bitrix OAuth refresh response is not an object")
                if data.get("error"):
                    self._repository.update_status(self._tenant_id, "error")
                    raise BitrixError(
                        f"Bitrix OAuth refresh failed: {data.get('error')}",
                        response_body=json.dumps(data, ensure_ascii=False),
                    )
                return data
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                body = exc.response.text if exc.response is not None else None
                if self._should_retry_status(status_code, attempt):
                    retry_reasons.append(f"HTTP {status_code}")
                    self._sleep_before_retry(attempt, f"HTTP {status_code}")
                    continue
                raise BitrixError(
                    "Bitrix OAuth refresh request failed",
                    status_code=status_code,
                    response_body=body,
                ) from exc
            except requests.RequestException as exc:
                if self._should_retry_connection(str(exc), attempt):
                    retry_reasons.append(str(exc))
                    self._sleep_before_retry(attempt, f"connection error: {exc}")
                    continue
                raise BitrixError(f"Bitrix OAuth refresh request failed: {exc}") from exc
            except ValueError as exc:
                raise BitrixError("Bitrix OAuth refresh response is not valid JSON") from exc

        raise BitrixError(
            "Bitrix OAuth refresh exhausted "
            f"{self._policy.max_attempts} attempts: {retry_reasons}"
        )

    def _current_token(self) -> BitrixOAuthToken:
        token = self._repository.get_by_tenant(self._tenant_id)
        if token is None:
            raise BitrixError(f"Bitrix OAuth token is not configured for tenant '{self._tenant_id}'")
        if token.status != "active":
            raise BitrixError(
                f"Bitrix OAuth token for tenant '{self._tenant_id}' is not active: {token.status}"
            )
        return token

    def _token_from_refresh_response(
        self,
        current: BitrixOAuthToken,
        response: dict[str, Any],
    ) -> BitrixOAuthToken:
        access_token = str(response.get("access_token") or "")
        if not access_token:
            raise BitrixError(
                "Bitrix OAuth refresh response is missing access_token",
                response_body=json.dumps(response, ensure_ascii=False),
            )

        client_endpoint = self._normalize_endpoint(
            str(response.get("client_endpoint") or current.client_endpoint)
        )
        bitrix_domain = self._resolve_bitrix_domain(
            current.bitrix_domain,
            str(response.get("domain") or ""),
            client_endpoint,
        )
        return BitrixOAuthToken(
            tenant_id=current.tenant_id,
            bitrix_member_id=str(response.get("member_id") or current.bitrix_member_id),
            bitrix_domain=bitrix_domain,
            client_endpoint=client_endpoint,
            access_token=access_token,
            refresh_token=str(response.get("refresh_token") or current.refresh_token),
            expires_at=self._resolve_expires_at(response),
            scope=str(response.get("scope") or current.scope),
            status="active",
            created_at=current.created_at,
            updated_at=current.updated_at,
        )

    def _needs_refresh(self, token: BitrixOAuthToken) -> bool:
        if token.expires_at <= 0:
            return True
        return token.expires_at <= int(time.time()) + self._refresh_skew_seconds

    def _require_refresh_credentials(self) -> None:
        if not self._client_id or not self._client_secret:
            raise BitrixError("Bitrix OAuth client credentials are not configured")

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

    def _sleep_before_retry(self, attempt: int, reason: str) -> None:
        delay = self._policy.backoff.delay_for(attempt)
        logger.warning(
            "Retrying Bitrix OAuth refresh after %s on attempt %d/%d (delay=%.1fs)",
            reason,
            attempt,
            self._policy.max_attempts,
            delay,
        )
        time.sleep(delay)

    @staticmethod
    def _is_expired_token_response(response: dict[str, Any]) -> bool:
        error = str(response.get("error") or "").lower()
        return error in _TOKEN_ERROR_MARKERS

    @staticmethod
    def _is_expired_token_error(exc: BitrixError) -> bool:
        body = (exc.response_body or "").lower()
        return exc.status_code in {401, 403} and any(marker in body for marker in _TOKEN_ERROR_MARKERS)

    @staticmethod
    def _resolve_expires_at(response: dict[str, Any]) -> int:
        expires = response.get("expires")
        if expires is not None:
            return int(expires)
        expires_in = response.get("expires_in")
        if expires_in is not None:
            return int(time.time()) + int(expires_in)
        return 0

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        if endpoint and not endpoint.endswith("/"):
            return f"{endpoint}/"
        return endpoint

    @staticmethod
    def _resolve_bitrix_domain(
        current_domain: str,
        response_domain: str,
        client_endpoint: str,
    ) -> str:
        if response_domain and not response_domain.startswith("oauth."):
            return response_domain
        if current_domain:
            return current_domain
        parsed = urlparse(client_endpoint)
        return parsed.netloc
