"""RunAuditService — full AI audit pipeline in one call.

Pipeline:
  1. WhatsApp timeline export  (Bitrix API, filtered by funnel/period/manager)
  2. WhatsApp feature extraction  (OpenAI)
  3. Feature aggregation  (local computation)
  4. Recommendation generation  (OpenAI)
"""
from __future__ import annotations

import json
import logging
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


class RunAuditService:
    """Chains WhatsApp timeline → features → aggregate → recommendations."""

    def __init__(
        self,
        bitrix_gateway: BitrixGateway,
        responses_gateway: ResponsesGateway,
        sink: JsonSink,
        call_gateway: BitrixGateway | None = None,
        transcription_gateway: TranscriptionGateway | None = None,
        file_downloader: FileDownloader | None = None,
    ) -> None:
        self._bitrix = bitrix_gateway
        self._responses = responses_gateway
        self._sink = sink
        self._call_gateway = call_gateway
        self._transcription = transcription_gateway
        self._file_downloader = file_downloader

    def execute(self, request: RunAuditRequest) -> None:
        timeline_dir = request.output_dir / "whatsapp-timeline"
        features_dir = request.output_dir / "whatsapp-features"
        analytics_dir = request.output_dir / "analytics"

        self._export_whatsapp_conversations(request, timeline_dir)

        logger.info("=== AUDIT STEP 2/4: WhatsApp feature extraction ===")
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
        AggregateFeatureService(sink=self._sink).execute(
            AggregateFeatureRequest(
                features_dir=features_dir / "features",
                output_dir=analytics_dir,
                limit=0,
            )
        )

        label = request.source_label or _build_label(request)
        logger.info("=== AUDIT STEP 4/4: Recommendation generation ===")
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
        logger.info("=== AUDIT STEP 1/4: WhatsApp timeline export ===")
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

        timeline_summary = self._timeline_summary(timeline_dir)
        if timeline_summary["rows_with_messages"] > 0:
            return

        logger.info(
            "Timeline export returned %d deals but 0 message-bearing conversations. "
            "Falling back to Open Lines export.",
            timeline_summary["row_count"],
        )
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
        self._ensure_timeline_has_messages(timeline_dir)

    def _ensure_timeline_has_messages(self, timeline_dir: Path) -> None:
        summary = self._timeline_summary(timeline_dir)
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

    def _timeline_summary(self, timeline_dir: Path) -> dict[str, int]:
        report_path = timeline_dir / "report.json"
        if not report_path.exists():
            raise ValueError(
                f"WhatsApp timeline report was not created: {report_path}"
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
            logger.info(
                "=== CALL AUDIT SKIPPED: X-Webhook-Url was not provided for /audit/run ==="
            )
            return
        if not self._transcription or not self._file_downloader:
            logger.info(
                "=== CALL AUDIT SKIPPED: downloader/transcription dependencies are missing ==="
            )
            return

        scan_dir = request.output_dir / "call-records-scan"
        recordings_dir = request.output_dir / "recordings"
        transcripts_dir = request.output_dir / "transcripts"
        call_features_dir = request.output_dir / "call-features"

        logger.info("=== AUDIT CALL STEP 1/4: Call records scan ===")
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
            logger.info(
                "=== CALL AUDIT STOPPED: no call recordings found for selected filters ==="
            )
            return

        logger.info("=== AUDIT CALL STEP 2/4: Recording download ===")
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
            logger.info(
                "=== CALL AUDIT STOPPED: recordings manifest contains no downloadable audio files ==="
            )
            return

        logger.info("=== AUDIT CALL STEP 3/4: Recording transcription ===")
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
            logger.info(
                "=== CALL AUDIT STOPPED: no transcripts were produced from downloaded recordings ==="
            )
            return

        logger.info("=== AUDIT CALL STEP 4/4: Call feature extraction ===")
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


def _build_label(request: RunAuditRequest) -> str:
    parts: list[str] = []
    if request.funnel_ids:
        parts.append("funnels_" + "_".join(request.funnel_ids))
    if request.date_from or request.date_to:
        parts.append(f"{request.date_from or 'start'}_{request.date_to or 'now'}")
    if request.responsible_id:
        parts.append(f"manager_{request.responsible_id}")
    return "_".join(parts) if parts else "full_audit"
