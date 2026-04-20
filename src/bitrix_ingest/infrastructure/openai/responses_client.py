"""OpenAI Responses API adapter — POST /v1/responses with structured JSON output."""
from __future__ import annotations

import json
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

_URL = "https://api.openai.com/v1/responses"


class OpenAiResponsesClient:
    def __init__(self, api_key: str, *, timeout: int = 120) -> None:
        self._api_key = api_key
        self._timeout = timeout

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
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
        logger.debug("Responses API call completed via %s", model)
        return response.json()

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
