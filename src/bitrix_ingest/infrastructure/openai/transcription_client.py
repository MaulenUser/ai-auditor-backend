"""OpenAI Audio Transcription adapter — POST /v1/audio/transcriptions."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

_URL = "https://api.openai.com/v1/audio/transcriptions"


class OpenAiTranscriptionClient:
    def __init__(self, api_key: str, *, timeout: int = 300) -> None:
        self._api_key = api_key
        self._timeout = timeout

    def transcribe(
        self,
        file_path: Path,
        model: str,
        language: str | None,
        prompt: str | None,
    ) -> dict[str, Any]:
        data: dict[str, str] = {"model": model, "response_format": "json"}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt

        with file_path.open("rb") as fh:
            response = requests.post(
                _URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                files={"file": (file_path.name, fh)},
                data=data,
                timeout=self._timeout,
            )

        if not response.ok:
            raise RuntimeError(
                f"OpenAI transcription HTTP {response.status_code}: {response.text[:500]}"
            )

        logger.debug("Transcribed %s via %s", file_path.name, model)
        return response.json()
