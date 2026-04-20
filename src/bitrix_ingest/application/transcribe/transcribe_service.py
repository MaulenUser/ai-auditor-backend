"""TranscribeRecordingsService — port of transcribe-recordings-openai.ps1."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ...domain.openai_usage import extract_usage_event, summarize_usage
from ...domain.transcripts import TranscriptError, TranscriptManifestEntry
from ..ports import JsonSink

logger = logging.getLogger(__name__)


class TranscriptionGateway(Protocol):
    def transcribe(
        self,
        file_path: Path,
        model: str,
        language: str | None,
        prompt: str | None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class TranscribeRecordingsRequest:
    manifest_path: Path
    output_dir: Path
    model: str = "gpt-4o-transcribe"
    language: str | None = None
    prompt: str | None = None
    limit: int = 0
    skip_existing: bool = False


def _safe_part(value: str) -> str:
    if not value or not value.strip():
        return "unknown"
    safe = re.sub(r"\s+", "_", value)
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", safe)
    safe = safe.strip("_")
    return safe or "unknown"


def _load_json_list(path: Path) -> list[Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], list):
        raw = raw[0]
    return raw if isinstance(raw, list) else [raw]


class TranscribeRecordingsService:
    def __init__(self, gateway: TranscriptionGateway, sink: JsonSink) -> None:
        self._gateway = gateway
        self._sink = sink

    def execute(self, request: TranscribeRecordingsRequest) -> None:
        if not request.manifest_path.exists():
            raise FileNotFoundError(f"Manifest not found: {request.manifest_path}")

        raw_entries = _load_json_list(request.manifest_path)
        entries = [
            e for e in raw_entries
            if isinstance(e, dict)
            and str(e.get("STATUS") or "") in ("downloaded", "skipped_existing")
            and str(e.get("FILE_PATH") or "").strip()
        ]
        if request.limit > 0:
            entries = entries[: request.limit]
        if not entries:
            raise ValueError("No audio files found in manifest for transcription.")

        text_dir = request.output_dir / "text"
        raw_dir = request.output_dir / "raw"
        for d in (request.output_dir, text_dir, raw_dir):
            d.mkdir(parents=True, exist_ok=True)

        manifest: list[TranscriptManifestEntry] = []
        errors: list[TranscriptError] = []
        usage_events: list[dict[str, Any]] = []

        for entry in entries:
            crm_id = str(entry.get("CRM_ACTIVITY_ID") or "")
            record_id = str(entry.get("RECORD_FILE_ID") or "")
            audio_path = Path(str(entry.get("FILE_PATH") or ""))

            stem = f"activity_{_safe_part(crm_id)}"
            if record_id:
                stem += f"__record_{_safe_part(record_id)}"
            text_path = text_dir / f"{stem}.txt"
            raw_path = raw_dir / f"{stem}.json"

            if not audio_path.exists():
                errors.append(TranscriptError(
                    crm_activity_id=crm_id, record_file_id=record_id,
                    source_file=str(audio_path), error="Audio file not found.",
                ))
                logger.info("Missing audio file for CRM_ACTIVITY_ID=%s", crm_id)
                continue

            if request.skip_existing and text_path.exists() and raw_path.exists():
                manifest.append(TranscriptManifestEntry(
                    crm_activity_id=crm_id, record_file_id=record_id,
                    audio_file_path=str(audio_path),
                    text_file_path=str(text_path), raw_file_path=str(raw_path),
                    status="skipped_existing", model=request.model,
                    language=request.language or "",
                ))
                logger.info("Skipped existing transcript for CRM_ACTIVITY_ID=%s", crm_id)
                continue

            try:
                response = self._gateway.transcribe(
                    file_path=audio_path, model=request.model,
                    language=request.language, prompt=request.prompt,
                )
                text = str(response.get("text") or "")
                text_path.write_text(text, encoding="utf-8")
                self._sink.write(raw_path, response)

                event = extract_usage_event(
                    response,
                    stage="transcription",
                    entity_type="crm_activity",
                    entity_id=crm_id,
                    source_file_path=str(audio_path),
                    model=request.model,
                    extra={"record_file_id": record_id},
                )
                if event:
                    usage_events.append(event)

                manifest.append(TranscriptManifestEntry(
                    crm_activity_id=crm_id, record_file_id=record_id,
                    audio_file_path=str(audio_path),
                    text_file_path=str(text_path), raw_file_path=str(raw_path),
                    status="transcribed", model=request.model,
                    language=request.language or "",
                    text_length=len(text),
                    input_tokens=event.get("input_tokens", 0) if event else 0,
                    output_tokens=event.get("output_tokens", 0) if event else 0,
                    total_tokens=event.get("total_tokens", 0) if event else 0,
                    audio_tokens=event.get("audio_input_tokens", 0) if event else 0,
                    text_tokens=event.get("text_input_tokens", 0) if event else 0,
                ))
                logger.info("Transcribed CRM_ACTIVITY_ID=%s", crm_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(TranscriptError(
                    crm_activity_id=crm_id, record_file_id=record_id,
                    source_file=str(audio_path), error=str(exc),
                ))
                logger.warning("Failed transcription for CRM_ACTIVITY_ID=%s: %s", crm_id, exc)

        self._sink.write(request.output_dir / "manifest.json", [e.to_dict() for e in manifest])
        self._sink.write(request.output_dir / "errors.json", [e.to_dict() for e in errors])
        self._sink.write(request.output_dir / "usage-events.json", usage_events)
        self._sink.write(request.output_dir / "usage-summary.json", summarize_usage(usage_events))

        logger.info("Transcription completed.")
        logger.info("Transcribed or skipped: %d", len(manifest))
        logger.info("Errors: %d", len(errors))
        logger.info("Files saved to %s", request.output_dir.resolve())
