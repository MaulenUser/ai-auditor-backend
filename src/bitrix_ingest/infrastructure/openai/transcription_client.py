"""OpenAI Audio Transcription adapter — POST /v1/audio/transcriptions."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from ..audit_trace import AuditTraceRecorder

logger = logging.getLogger(__name__)

_URL = "https://api.openai.com/v1/audio/transcriptions"


class OpenAiTranscriptionClient:
    def __init__(
        self,
        api_key: str,
        *,
        timeout: int = 300,
        trace: AuditTraceRecorder | None = None,
        trace_name: str = "openai.transcription",
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._trace = trace
        self._trace_name = trace_name

    def transcribe(
        self,
        file_path: Path,
        model: str,
        language: str | None,
        prompt: str | None,
    ) -> dict[str, Any]:
        started_at = datetime.now(tz=timezone.utc)
        data: dict[str, str] = {"model": model, "response_format": "json"}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt

        try:
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
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            self._record_trace(
                started_at=started_at,
                status="error",
                file_path=file_path,
                model=model,
                language=language,
                prompt=prompt,
                error=exc,
            )
            raise

        self._record_trace(
            started_at=started_at,
            status="ok",
            file_path=file_path,
            model=model,
            language=language,
            prompt=prompt,
            transcript_chars=len(str(payload.get("text") or "")),
        )
        logger.debug("Transcribed %s via %s", file_path.name, model)
        return payload

    def _record_trace(
        self,
        *,
        started_at: datetime,
        status: str,
        file_path: Path,
        model: str,
        language: str | None,
        prompt: str | None,
        transcript_chars: int = 0,
        error: BaseException | None = None,
    ) -> None:
        if not self._trace:
            return
        file_size = file_path.stat().st_size if file_path.exists() else None
        self._trace.record_operation(
            "openai_transcription",
            self._trace_name,
            started_at=started_at,
            finished_at=datetime.now(tz=timezone.utc),
            status=status,
            details={
                "model": model,
                "file_path": file_path.as_posix(),
                "file_name": file_path.name,
                "file_size_bytes": file_size,
                "language": language or "",
                "prompt_chars": len(prompt or ""),
                "transcript_chars": transcript_chars,
            },
            error=error,
        )
