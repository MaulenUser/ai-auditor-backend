"""ExtractCallFeaturesService — port of extract-call-features-openai.ps1."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ...domain.features import CallFeatureReportRow, call_features_schema
from ...domain.openai_usage import extract_usage_event, summarize_usage
from ..ports import JsonSink

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You analyze sales call transcripts for an MVP analytics pipeline.

Return JSON only and follow the schema exactly.
Use only evidence from the transcript and provided metadata.
If something is not clear from the transcript, use "unknown".
Do not invent prices, budgets, timings, or client intent.
Free-text fields must be in Russian.
Tags must be short Russian labels.

Interpretation rules:
- target_client: the call is relevant to the company's target service offer.
- not_target_client: the caller/request is outside the company's target service or is a partner/counterparty instead of a client.
- technical_or_short: the call is too short or too poor in content for meaningful sales analysis.
- callback_requested: a callback was explicitly requested.
- follow_up: there is a clear next step but not enough evidence for strong qualified interest.
- qualified_interest: the client shows meaningful service interest and the call has real sales value.
- non_sales_call=true when the call is administrative, partner-related, technical, or otherwise not a real sales conversation."""


class ResponsesGateway(Protocol):
    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]: ...

    @staticmethod
    def extract_output_text(response: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class ExtractCallFeaturesRequest:
    transcript_manifest_path: Path
    output_dir: Path
    call_metadata_path: Path | None = None
    activity_metadata_path: Path | None = None
    model: str = "gpt-4o-mini"
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


def _call_direction(metadata: dict[str, Any] | None, activity: dict[str, Any] | None) -> str:
    direction = str((activity or {}).get("DIRECTION") or "")
    if direction == "1":
        return "inbound"
    if direction == "2":
        return "outbound"
    subject = str(
        (metadata or {}).get("SUBJECT") or (activity or {}).get("SUBJECT") or ""
    ).lower()
    if "входящий" in subject:
        return "inbound"
    if "исходящий" in subject:
        return "outbound"
    if "inbound" in subject:
        return "inbound"
    if "outbound" in subject:
        return "outbound"
    return "unknown"


class ExtractCallFeaturesService:
    def __init__(self, gateway: ResponsesGateway, sink: JsonSink) -> None:
        self._gateway = gateway
        self._sink = sink

    def execute(self, request: ExtractCallFeaturesRequest) -> None:
        if not request.transcript_manifest_path.exists():
            raise FileNotFoundError(
                f"Transcript manifest not found: {request.transcript_manifest_path}"
            )

        raw_entries = _load_json_list(request.transcript_manifest_path)
        entries = [
            e for e in raw_entries
            if isinstance(e, dict)
            and str(e.get("STATUS") or "") in ("transcribed", "skipped_existing")
            and str(e.get("TEXT_FILE_PATH") or "").strip()
        ]
        if request.limit > 0:
            entries = entries[: request.limit]
        if not entries:
            raise ValueError("No transcripts available for feature extraction.")

        meta_index: dict[str, dict[str, Any]] = {}
        activity_index: dict[str, dict[str, Any]] = {}
        if request.call_metadata_path and request.call_metadata_path.exists():
            for item in _load_json_list(request.call_metadata_path):
                if isinstance(item, dict):
                    meta_index[str(item.get("CRM_ACTIVITY_ID") or "")] = item
        if request.activity_metadata_path and request.activity_metadata_path.exists():
            for item in _load_json_list(request.activity_metadata_path):
                if isinstance(item, dict):
                    activity_index[str(item.get("ID") or "")] = item

        features_dir = request.output_dir / "features"
        raw_dir = request.output_dir / "raw"
        for d in (request.output_dir, features_dir, raw_dir):
            d.mkdir(parents=True, exist_ok=True)

        report_rows: list[CallFeatureReportRow] = []
        errors: list[dict[str, Any]] = []
        usage_events: list[dict[str, Any]] = []
        schema = call_features_schema()

        for entry in entries:
            crm_id = str(entry.get("CRM_ACTIVITY_ID") or "")
            record_id = str(entry.get("RECORD_FILE_ID") or "")
            transcript_path = Path(str(entry.get("TEXT_FILE_PATH") or ""))

            stem = f"activity_{_safe_part(crm_id)}"
            if record_id:
                stem += f"__record_{_safe_part(record_id)}"
            feature_path = features_dir / f"{stem}.json"
            raw_path = raw_dir / f"{stem}.json"

            if not transcript_path.exists():
                errors.append({
                    "CRM_ACTIVITY_ID": crm_id, "RECORD_FILE_ID": record_id,
                    "Error": "Transcript file not found.",
                })
                logger.info("Missing transcript for CRM_ACTIVITY_ID=%s", crm_id)
                continue

            if request.skip_existing and feature_path.exists() and raw_path.exists():
                existing = json.loads(feature_path.read_text(encoding="utf-8"))
                report_rows.append(CallFeatureReportRow(
                    crm_activity_id=crm_id, record_file_id=record_id,
                    feature_file_path=str(feature_path), raw_file_path=str(raw_path),
                    status="skipped_existing",
                    primary_topic=str(existing.get("primary_topic") or ""),
                    relevance=str(existing.get("relevance_to_company") or ""),
                    outcome_status=str((existing.get("outcome") or {}).get("status") or ""),
                    client_interest=str(
                        (existing.get("sentiment") or {}).get("client_interest_level") or ""
                    ),
                    short_or_low_content=bool(
                        (existing.get("quality_flags") or {}).get("short_or_low_content")
                    ),
                ))
                logger.info("Skipped existing features for CRM_ACTIVITY_ID=%s", crm_id)
                continue

            transcript_text = transcript_path.read_text(encoding="utf-8").strip()
            meta = meta_index.get(crm_id)
            activity = activity_index.get(crm_id)
            ctx = {
                "crm_activity_id": crm_id,
                "call_id": str((meta or {}).get("CALL_ID") or ""),
                "record_file_id": record_id,
                "responsible_id": str((meta or {}).get("RESPONSIBLE_ID") or ""),
                "phone_number": str((meta or {}).get("PHONE_NUMBER") or ""),
                "started_at": str((meta or {}).get("START_TIME") or ""),
                "subject": str((meta or {}).get("SUBJECT") or ""),
                "call_direction": _call_direction(meta, activity),
            }
            user_prompt = (
                f"Metadata:\n"
                f"- CRM activity ID: {ctx['crm_activity_id']}\n"
                f"- Call direction: {ctx['call_direction']}\n"
                f"- Manager ID: {ctx['responsible_id']}\n"
                f"- Phone number: {ctx['phone_number']}\n"
                f"- Subject: {ctx['subject']}\n"
                f"- Started at: {ctx['started_at']}\n\n"
                f"Transcript:\n{transcript_text}"
            )

            try:
                response = self._gateway.complete(
                    system_prompt=_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    model=request.model,
                    schema_name="sales_call_features",
                    schema=schema,
                )
                parsed = json.loads(self._gateway.extract_output_text(response))
                event = extract_usage_event(
                    response,
                    stage="call_feature_extraction",
                    entity_type="crm_activity",
                    entity_id=crm_id,
                    source_file_path=str(transcript_path),
                    model=request.model,
                    extra={"record_file_id": record_id},
                )
                if event:
                    usage_events.append(event)

                final_feature = {
                    "schema_version": "mvp_call_features_v1",
                    "source": {
                        "crm_activity_id": crm_id,
                        "call_id": ctx["call_id"],
                        "record_file_id": record_id,
                        "responsible_id": ctx["responsible_id"],
                        "phone_number": ctx["phone_number"],
                        "started_at": ctx["started_at"],
                        "call_direction": ctx["call_direction"],
                        "subject": ctx["subject"],
                        "transcript_file_path": str(transcript_path),
                    },
                    **parsed,
                }
                self._sink.write(feature_path, final_feature)
                self._sink.write(raw_path, response)

                report_rows.append(CallFeatureReportRow(
                    crm_activity_id=crm_id, record_file_id=record_id,
                    feature_file_path=str(feature_path), raw_file_path=str(raw_path),
                    status="extracted",
                    primary_topic=str(parsed.get("primary_topic") or ""),
                    relevance=str(parsed.get("relevance_to_company") or ""),
                    outcome_status=str((parsed.get("outcome") or {}).get("status") or ""),
                    client_interest=str(
                        (parsed.get("sentiment") or {}).get("client_interest_level") or ""
                    ),
                    short_or_low_content=bool(
                        (parsed.get("quality_flags") or {}).get("short_or_low_content")
                    ),
                    input_tokens=event.get("input_tokens", 0) if event else 0,
                    output_tokens=event.get("output_tokens", 0) if event else 0,
                    total_tokens=event.get("total_tokens", 0) if event else 0,
                    cached_tokens=event.get("cached_tokens", 0) if event else 0,
                    reasoning_tokens=event.get("reasoning_tokens", 0) if event else 0,
                ))
                logger.info("Extracted features for CRM_ACTIVITY_ID=%s", crm_id)
            except Exception as exc:  # noqa: BLE001
                errors.append({
                    "CRM_ACTIVITY_ID": crm_id, "RECORD_FILE_ID": record_id, "Error": str(exc),
                })
                logger.warning("Failed feature extraction for CRM_ACTIVITY_ID=%s: %s", crm_id, exc)

        self._sink.write(
            request.output_dir / "feature-report.json", [r.to_dict() for r in report_rows]
        )
        self._sink.write(request.output_dir / "errors.json", errors)
        self._sink.write(request.output_dir / "usage-events.json", usage_events)
        self._sink.write(request.output_dir / "usage-summary.json", summarize_usage(usage_events))

        logger.info("Feature extraction completed.")
        logger.info("Extracted or skipped: %d", len(report_rows))
        logger.info("Errors: %d", len(errors))
        logger.info("Files saved to %s", request.output_dir.resolve())
