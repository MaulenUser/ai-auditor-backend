"""RunAuditService — full AI audit pipeline in one call.

Pipeline:
  1. WhatsApp Open Lines export  (Bitrix API, filtered by funnel/period/manager)
  2. WhatsApp feature extraction  (OpenAI)
  3. Feature aggregation  (local computation)
  4. Recommendation generation  (OpenAI)
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..analytics import AggregateFeatureRequest, AggregateFeatureService
from ..analytics import GenerateRecommendationsRequest, GenerateRecommendationsService
from ..call_features import ExtractCallFeaturesRequest, ExtractCallFeaturesService
from ..call_records import CallRecordsScanRequest, CallRecordsScanService
from ..ports import BitrixGateway, JsonSink
from ..recordings import DownloadRecordingsRequest, DownloadRecordingsService
from ..transcribe import TranscribeRecordingsRequest, TranscribeRecordingsService
from ..whatsapp import WhatsAppExportRequest, WhatsAppExportService
from ..whatsapp_features import ExtractWhatsAppFeaturesRequest, ExtractWhatsAppFeaturesService
from ..whatsapp_timeline import WhatsAppTimelineExportRequest, WhatsAppTimelineExportService
from ...infrastructure.audit_trace import AuditTraceRecorder

logger = logging.getLogger(__name__)


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


class TranscriptionGateway(Protocol):
    def transcribe(
        self,
        file_path: Path,
        model: str,
        language: str | None,
        prompt: str | None,
    ) -> dict[str, Any]: ...


class FileDownloader(Protocol):
    def download(self, url: str, target_path: Path) -> None: ...


@dataclass(frozen=True)
class RunAuditRequest:
    output_dir: Path
    funnel_ids: list[str] | None = None  # None = all funnels; list = OR across selected
    date_from: str | None = None
    date_to: str | None = None
    responsible_id: str | None = None
    limit: int = 0
    model: str = "gpt-4o-mini"
    recommendations_model: str = "gpt-4o"
    source_label: str = ""
    page_delay: float = 0.3  # seconds between deals to avoid Bitrix 503 rate-limits


class RunAuditService:
    """Chains WhatsApp Open Lines → features → aggregate → recommendations."""

    def __init__(
        self,
        bitrix_gateway: BitrixGateway,
        responses_gateway: ResponsesGateway,
        sink: JsonSink,
        call_gateway: BitrixGateway | None = None,
        transcription_gateway: TranscriptionGateway | None = None,
        file_downloader: FileDownloader | None = None,
        trace: AuditTraceRecorder | None = None,
    ) -> None:
        self._bitrix = bitrix_gateway
        self._responses = responses_gateway
        self._sink = sink
        self._call_gateway = call_gateway
        self._transcription = transcription_gateway
        self._file_downloader = file_downloader
        self._trace = trace

    def execute(self, request: RunAuditRequest) -> None:
        timeline_dir = request.output_dir / "whatsapp-timeline"
        features_dir = request.output_dir / "whatsapp-features"
        analytics_dir = request.output_dir / "analytics"

        with self._span(
            "stage",
            "whatsapp_export",
            details={"output_dir": timeline_dir},
        ):
            self._export_whatsapp_conversations(request, timeline_dir)
        with self._span(
            "stage",
            "crm_outcome_summary_generation",
            details={"output_dir": analytics_dir},
        ):
            self._write_crm_outcome_summary(request, timeline_dir, analytics_dir)

        logger.info("=== AUDIT STEP 2/4: WhatsApp feature extraction ===")
        with self._span(
            "stage",
            "whatsapp_feature_extraction",
            details={"output_dir": features_dir, "model": request.model},
        ):
            ExtractWhatsAppFeaturesService(
                gateway=self._responses, sink=self._sink,
            ).execute(
                ExtractWhatsAppFeaturesRequest(
                    output_dir=features_dir,
                    conversation_report_path=timeline_dir / "report.json",
                    conversation_dir=timeline_dir / "conversations",
                    model=request.model,
                    limit=0,
                    skip_existing=False,
                )
            )
        self._ensure_features_exist(features_dir)

        logger.info("=== AUDIT STEP 3/4: Feature aggregation ===")
        with self._span(
            "stage",
            "feature_aggregation",
            details={"output_dir": analytics_dir},
        ):
            AggregateFeatureService(sink=self._sink).execute(
                AggregateFeatureRequest(
                    features_dir=features_dir / "features",
                    output_dir=analytics_dir,
                    limit=0,
                )
            )

        label = request.source_label or _build_label(request)
        logger.info("=== AUDIT STEP 4/4: Recommendation generation ===")
        with self._span(
            "stage",
            "recommendation_generation",
            details={
                "output_dir": analytics_dir,
                "model": request.recommendations_model,
                "source_label": label,
            },
        ):
            GenerateRecommendationsService(
                gateway=self._responses, sink=self._sink,
            ).execute(
                GenerateRecommendationsRequest(
                    aggregate_path=analytics_dir / "aggregate.json",
                    output_dir=analytics_dir,
                    model=request.recommendations_model,
                    source_label=label,
                )
            )

        self._run_call_audit_if_configured(request)

        logger.info("=== AUDIT COMPLETE. Output: %s ===", request.output_dir.resolve())

    def _export_whatsapp_conversations(
        self,
        request: RunAuditRequest,
        timeline_dir: Path,
    ) -> None:
        logger.info("=== AUDIT STEP 1/4: WhatsApp Open Lines export ===")
        with self._span(
            "stage",
            "whatsapp_openlines_export",
            details={"output_dir": timeline_dir, "limit": request.limit},
        ):
            WhatsAppExportService(
                gateway=self._bitrix, sink=self._sink,
            ).execute(
                WhatsAppExportRequest(
                    output_dir=timeline_dir,
                    limit=request.limit,
                    date_from=request.date_from,
                    date_to=request.date_to,
                    include_system_messages=True,
                    category_ids=request.funnel_ids,
                    responsible_id=request.responsible_id,
                )
            )

        conversation_summary = self._conversation_summary(timeline_dir)
        if conversation_summary["rows_with_messages"] > 0:
            return

        self._note(
            "stage",
            "whatsapp_timeline_fallback_triggered",
            details=conversation_summary,
        )
        logger.info(
            "Open Lines export returned %d deals but 0 message-bearing conversations. "
            "Falling back to timeline export.",
            conversation_summary["row_count"],
        )
        with self._span(
            "stage",
            "whatsapp_timeline_export",
            details={"output_dir": timeline_dir, "limit": request.limit},
        ):
            WhatsAppTimelineExportService(
                gateway=self._bitrix, sink=self._sink,
            ).execute(
                WhatsAppTimelineExportRequest(
                    output_dir=timeline_dir,
                    limit=request.limit,
                    date_from=request.date_from,
                    date_to=request.date_to,
                    category_ids=request.funnel_ids,
                    responsible_id=request.responsible_id,
                )
            )
        self._ensure_conversations_have_messages(timeline_dir)

    def _ensure_conversations_have_messages(self, timeline_dir: Path) -> None:
        summary = self._conversation_summary(timeline_dir)
        if summary["row_count"] == 0:
            raise ValueError(
                "No WhatsApp deals were exported for the selected filters."
            )
        if summary["rows_with_messages"] > 0:
            return

        raise ValueError(
            "WhatsApp deals were found, but all exported conversations are empty "
            "(0 messages). Audit cannot continue for the selected filters."
        )

    def _conversation_summary(self, timeline_dir: Path) -> dict[str, int]:
        report_path = timeline_dir / "report.json"
        if not report_path.exists():
            raise ValueError(
                f"WhatsApp report was not created: {report_path}"
            )

        report = json.loads(report_path.read_text(encoding="utf-8"))
        rows = report.get("rows") or []
        rows_with_messages = [
            row for row in rows
            if int(row.get("total_messages") or 0) > 0
        ]
        return {
            "row_count": len(rows),
            "rows_with_messages": len(rows_with_messages),
        }

    def _ensure_features_exist(self, features_dir: Path) -> None:
        feature_files = list((features_dir / "features").glob("*.json"))
        if feature_files:
            return

        errors_path = features_dir / "errors.json"
        if errors_path.exists():
            errors = json.loads(errors_path.read_text(encoding="utf-8"))
            if isinstance(errors, list) and errors:
                first = errors[0]
                message = str(first.get("Error") or "Unknown feature extraction error")
                raise ValueError(
                    "No feature files were produced during WhatsApp feature extraction. "
                    f"First error: {message}"
                )

        raise ValueError(
            f"No feature JSON files found in {features_dir / 'features'}"
        )

    def _run_call_audit_if_configured(self, request: RunAuditRequest) -> None:
        if not self._call_gateway:
            self._note(
                "call_pipeline",
                "call_pipeline_skipped",
                status="skipped",
                details={"reason": "crm_webhook_missing"},
            )
            logger.info(
                "=== CALL AUDIT SKIPPED: X-Webhook-Url was not provided for /audit/run ==="
            )
            return
        if not self._transcription or not self._file_downloader:
            self._note(
                "call_pipeline",
                "call_pipeline_skipped",
                status="skipped",
                details={"reason": "call_dependencies_missing"},
            )
            logger.info(
                "=== CALL AUDIT SKIPPED: downloader/transcription dependencies are missing ==="
            )
            return

        scan_dir = request.output_dir / "call-records-scan"
        recordings_dir = request.output_dir / "recordings"
        transcripts_dir = request.output_dir / "transcripts"
        call_features_dir = request.output_dir / "call-features"

        logger.info("=== AUDIT CALL STEP 1/4: Call records scan ===")
        with self._span(
            "call_stage",
            "call_records_scan",
            details={"output_dir": scan_dir, "limit": request.limit},
        ):
            CallRecordsScanService(
                gateway=self._call_gateway, sink=self._sink,
            ).execute(
                CallRecordsScanRequest(
                    output_dir=scan_dir,
                    limit=request.limit,
                    date_from=request.date_from,
                    date_to=request.date_to,
                    responsible_id=request.responsible_id,
                )
            )

        candidate_count = self._json_list_count(scan_dir / "recording-candidates.json")
        if candidate_count == 0:
            self._note(
                "call_pipeline",
                "call_pipeline_stopped",
                status="skipped",
                details={"reason": "no_recordings_found", "scan_dir": scan_dir},
            )
            logger.info(
                "=== CALL AUDIT STOPPED: no call recordings found for selected filters ==="
            )
            return

        logger.info("=== AUDIT CALL STEP 2/4: Recording download ===")
        with self._span(
            "call_stage",
            "recording_download",
            details={"output_dir": recordings_dir},
        ):
            DownloadRecordingsService(
                downloader=self._file_downloader, sink=self._sink,
            ).execute(
                DownloadRecordingsRequest(
                    source_json_path=scan_dir / "recording-candidates.json",
                    output_dir=recordings_dir,
                    skip_existing=False,
                )
            )

        transcript_candidates = self._manifest_entry_count(recordings_dir / "manifest.json")
        if transcript_candidates == 0:
            self._note(
                "call_pipeline",
                "call_pipeline_stopped",
                status="skipped",
                details={"reason": "no_downloadable_audio", "recordings_dir": recordings_dir},
            )
            logger.info(
                "=== CALL AUDIT STOPPED: recordings manifest contains no downloadable audio files ==="
            )
            return

        logger.info("=== AUDIT CALL STEP 3/4: Recording transcription ===")
        with self._span(
            "call_stage",
            "recording_transcription",
            details={"output_dir": transcripts_dir},
        ):
            TranscribeRecordingsService(
                gateway=self._transcription, sink=self._sink,
            ).execute(
                TranscribeRecordingsRequest(
                    manifest_path=recordings_dir / "manifest.json",
                    output_dir=transcripts_dir,
                    limit=0,
                    skip_existing=False,
                )
            )

        transcript_count = self._manifest_entry_count(transcripts_dir / "manifest.json")
        if transcript_count == 0:
            self._note(
                "call_pipeline",
                "call_pipeline_stopped",
                status="skipped",
                details={"reason": "no_transcripts_produced", "transcripts_dir": transcripts_dir},
            )
            logger.info(
                "=== CALL AUDIT STOPPED: no transcripts were produced from downloaded recordings ==="
            )
            return

        logger.info("=== AUDIT CALL STEP 4/4: Call feature extraction ===")
        with self._span(
            "call_stage",
            "call_feature_extraction",
            details={"output_dir": call_features_dir, "model": request.model},
        ):
            ExtractCallFeaturesService(
                gateway=self._responses, sink=self._sink,
            ).execute(
                ExtractCallFeaturesRequest(
                    transcript_manifest_path=transcripts_dir / "manifest.json",
                    output_dir=call_features_dir,
                    call_metadata_path=scan_dir / "recording-candidates.json",
                    activity_metadata_path=scan_dir / "activities.source.json",
                    model=request.model,
                    limit=0,
                    skip_existing=False,
                )
            )
        self._ensure_call_features_exist(call_features_dir)

    @staticmethod
    def _json_list_count(path: Path) -> int:
        if not path.exists():
            return 0
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], list):
            raw = raw[0]
        if not isinstance(raw, list):
            raw = [raw]
        return len([item for item in raw if isinstance(item, dict)])

    @staticmethod
    def _manifest_entry_count(path: Path) -> int:
        if not path.exists():
            return 0
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], list):
            raw = raw[0]
        if not isinstance(raw, list):
            raw = [raw]
        return len([
            item for item in raw
            if isinstance(item, dict)
            and str(item.get("STATUS") or "") not in ("", "error")
        ])

    def _ensure_call_features_exist(self, features_dir: Path) -> None:
        feature_files = list((features_dir / "features").glob("*.json"))
        if feature_files:
            return

        errors_path = features_dir / "errors.json"
        if errors_path.exists():
            errors = json.loads(errors_path.read_text(encoding="utf-8"))
            if isinstance(errors, list) and errors:
                first = errors[0]
                message = str(first.get("Error") or "Unknown call feature extraction error")
                raise ValueError(
                    "No call feature files were produced during call feature extraction. "
                    f"First error: {message}"
                )

        raise ValueError(
            f"No call feature JSON files found in {features_dir / 'features'}"
        )

    def _write_crm_outcome_summary(
        self,
        request: RunAuditRequest,
        timeline_dir: Path,
        analytics_dir: Path,
    ) -> None:
        deals_path = timeline_dir / "deals.source.json"
        if not deals_path.exists():
            logger.warning(
                "CRM outcome summary skipped: deals source file not found at %s",
                deals_path,
            )
            return

        raw = json.loads(deals_path.read_text(encoding="utf-8"))
        deals = raw if isinstance(raw, list) else []
        summary = self._build_crm_outcome_summary(request, deals, timeline_dir)
        analytics_dir.mkdir(parents=True, exist_ok=True)
        self._sink.write(analytics_dir / "crm_outcome_summary.json", summary)

    def _build_crm_outcome_summary(
        self,
        request: RunAuditRequest,
        deals: list[dict[str, Any]],
        timeline_dir: Path,
    ) -> dict[str, Any]:
        semantic_labels = {"S": "successful", "F": "failed", "P": "in_progress"}
        overall: dict[str, Any] = {
            "total": len(deals),
            "successful": 0,
            "failed": 0,
            "in_progress": 0,
            "unknown": 0,
            "successful_amount": 0.0,
            "failed_amount": 0.0,
            "in_progress_amount": 0.0,
        }
        by_manager: dict[str, dict[str, Any]] = {}
        by_stage: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "semantic_ids": defaultdict(int)}
        )

        for deal in deals:
            manager_id = str(deal.get("ASSIGNED_BY_ID") or "")
            stage_id = str(deal.get("STAGE_ID") or "")
            semantic = str(deal.get("STAGE_SEMANTIC_ID") or "").strip().upper()
            bucket = semantic_labels.get(semantic, "unknown")
            amount = _as_float(deal.get("OPPORTUNITY"))

            overall[bucket] += 1
            overall[f"{bucket}_amount"] += amount

            manager = by_manager.setdefault(
                manager_id,
                {
                    "assigned_by_id": manager_id,
                    "total": 0,
                    "successful": 0,
                    "failed": 0,
                    "in_progress": 0,
                    "unknown": 0,
                    "successful_amount": 0.0,
                    "failed_amount": 0.0,
                    "in_progress_amount": 0.0,
                    "stages": defaultdict(int),
                },
            )
            manager["total"] += 1
            manager[bucket] += 1
            manager[f"{bucket}_amount"] += amount
            manager["stages"][stage_id] += 1

            by_stage[stage_id]["count"] += 1
            by_stage[stage_id]["semantic_ids"][semantic or ""] += 1

        return {
            "generated_at": _now_iso(),
            "source": {
                "timeline_dir": str(timeline_dir),
                "deals_source_path": str(timeline_dir / "deals.source.json"),
            },
            "request": {
                "funnel_ids": request.funnel_ids or [],
                "date_from": request.date_from,
                "date_to": request.date_to,
                "responsible_id": request.responsible_id,
                "limit": request.limit,
            },
            "crm_truth_rule": {
                "successful": "STAGE_SEMANTIC_ID = 'S'",
                "failed": "STAGE_SEMANTIC_ID = 'F'",
                "in_progress": "STAGE_SEMANTIC_ID = 'P'",
            },
            "overall": overall,
            "by_manager": [
                {
                    **manager,
                    "stages": dict(
                        sorted(
                            manager["stages"].items(),
                            key=lambda item: (-item[1], item[0]),
                        )
                    ),
                }
                for _, manager in sorted(
                    by_manager.items(),
                    key=lambda item: (-item[1]["total"], item[0]),
                )
            ],
            "by_stage": [
                {
                    "stage_id": stage_id,
                    "count": info["count"],
                    "semantic_ids": dict(sorted(info["semantic_ids"].items())),
                }
                for stage_id, info in sorted(
                    by_stage.items(),
                    key=lambda item: (-item[1]["count"], item[0]),
                )
            ],
        }

    def _span(
        self,
        event_type: str,
        name: str,
        *,
        details: dict[str, Any] | None = None,
    ):
        if not self._trace:
            return nullcontext()
        return self._trace.span(event_type, name, details=details)

    def _note(
        self,
        event_type: str,
        name: str,
        *,
        status: str = "ok",
        details: dict[str, Any] | None = None,
    ) -> None:
        if not self._trace:
            return
        self._trace.record_instant(
            event_type,
            name,
            status=status,
            details=details,
        )


def _build_label(request: RunAuditRequest) -> str:
    parts: list[str] = []
    if request.funnel_ids:
        parts.append("funnels_" + "_".join(request.funnel_ids))
    if request.date_from or request.date_to:
        parts.append(f"{request.date_from or 'start'}_{request.date_to or 'now'}")
    if request.responsible_id:
        parts.append(f"manager_{request.responsible_id}")
    return "_".join(parts) if parts else "full_audit"


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat()
