"""OpenAI Responses API adapter — POST /v1/responses with structured JSON output."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import requests

from ..audit_trace import AuditTraceRecorder

logger = logging.getLogger(__name__)

_URL = "https://api.openai.com/v1/responses"


class OpenAiResponsesClient:
    def __init__(
        self,
        api_key: str,
        *,
        timeout: int = 120,
        trace: AuditTraceRecorder | None = None,
        trace_name: str = "openai.responses",
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._trace = trace
        self._trace_name = trace_name

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        started_at = datetime.now(tz=timezone.utc)
        body = {
            "model": model,
            "store": False,
            "input": [
                {
                    "role": "developer",
                    "content": [{"type": "input_text", "text": system_prompt}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_prompt}],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        try:
            response = requests.post(
                _URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                timeout=self._timeout,
            )
            if not response.ok:
                raise RuntimeError(
                    f"OpenAI responses HTTP {response.status_code}: {response.text[:500]}"
                )
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            self._record_trace(
                started_at=started_at,
                status="error",
                model=model,
                schema_name=schema_name,
                input_chars=len(system_prompt) + len(user_prompt),
                error=exc,
            )
            raise
        self._record_trace(
            started_at=started_at,
            status="ok",
            model=model,
            schema_name=schema_name,
            input_chars=len(system_prompt) + len(user_prompt),
            response_id=str(payload.get("id") or ""),
            output_text_chars=len(str(payload.get("output_text") or "")),
        )
        logger.debug("Responses API call completed via %s", model)
        return payload

    @staticmethod
    def extract_output_text(response: dict[str, Any]) -> str:
        if text := str(response.get("output_text") or "").strip():
            return text
        for item in response.get("output") or []:
            if item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if content.get("type") == "output_text":
                    text = str(content.get("text") or "").strip()
                    if text:
                        return text
        raise ValueError(
            f"Structured output text not found in response: {json.dumps(response)[:300]}"
        )

    def _record_trace(
        self,
        *,
        started_at: datetime,
        status: str,
        model: str,
        schema_name: str,
        input_chars: int,
        response_id: str = "",
        output_text_chars: int = 0,
        error: BaseException | None = None,
    ) -> None:
        if not self._trace:
            return
        self._trace.record_operation(
            "openai_response",
            self._trace_name,
            started_at=started_at,
            finished_at=datetime.now(tz=timezone.utc),
            status=status,
            details={
                "model": model,
                "schema_name": schema_name,
                "input_chars": input_chars,
                "response_id": response_id,
                "output_text_chars": output_text_chars,
            },
            error=error,
        )
