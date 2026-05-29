"""Run the executive report pipeline with one shared CRM scope."""
from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from ..call_records import CallRecordsScanRequest, CallRecordsScanService
from ..date_range import within_any_record_datetime_range
from ..executive_report import BuildExecutiveReportRequest, BuildExecutiveReportService
from ..ports import BitrixGateway, FileDownloader, JsonSink
from ..recordings import DownloadRecordingsRequest, DownloadRecordingsService
from ..sales_quality import AnalyzeSalesQualityRequest, AnalyzeSalesQualityService
from ..transcribe import TranscribeRecordingsRequest, TranscribeRecordingsService
from ..whatsapp import WhatsAppExportRequest, WhatsAppExportService
from ..whatsapp_timeline import WhatsAppTimelineExportRequest, WhatsAppTimelineExportService

logger = logging.getLogger(__name__)

_DEAL_SELECT = [
    "ID",
    "TITLE",
    "STAGE_ID",
    "STAGE_SEMANTIC_ID",
    "CATEGORY_ID",
    "ASSIGNED_BY_ID",
    "OPPORTUNITY",
    "CURRENCY_ID",
    "CONTACT_ID",
    "COMPANY_ID",
    "SOURCE_ID",
    "DATE_CREATE",
    "DATE_MODIFY",
    "CLOSEDATE",
    "CLOSED",
    "LAST_COMMUNICATION_TIME",
    "LOSS_REASON_ID",
    "LOSS_COMMENT",
    "UTM_SOURCE",
    "UTM_MEDIUM",
    "UTM_CAMPAIGN",
]


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


@dataclass(frozen=True)
class RunExecutivePipelineRequest:
    sales_quality_dir: Path = Path("export/sales-quality")
    executive_report_dir: Path = Path("export/executive-report")
    whatsapp_dir: Path = Path("export/whatsapp-timeline")
    call_scan_dir: Path = Path("export/call-records-scan")
    recordings_dir: Path = Path("export/recordings")
    date_from: str | None = None
    date_to: str | None = None
    category_ids: list[str] | None = None
    responsible_ids: list[str] | None = None
    deal_ids: list[str] | None = None
    limit: int = 0
    model: str = "gpt-4o-mini"
    transcription_model: str = "gpt-4o-transcribe"
    transcription_language: str | None = None
    transcription_prompt: str | None = None
    slow_response_threshold_sec: int = 900
    max_chars_per_item: int = 24000
    average_ticket_kzt: float | None = None
    expected_conversion_pct: float | None = None
    portal_base_url: str = "https://sapaplast.bitrix24.kz"
    max_reanimation_cards: int = 100
    include_whatsapp: bool = True
    include_whatsapp_audio: bool = False
    include_calls: bool = True
    reset_outputs: bool = True


class RunExecutivePipelineService:
    """Refresh source data, AI features, and the final executive report."""

    def __init__(
        self,
        *,
        crm_gateway: BitrixGateway,
        whatsapp_gateway: BitrixGateway,
        responses_gateway: ResponsesGateway,
        sink: JsonSink,
        transcription_gateway: TranscriptionGateway | None = None,
        file_downloader: FileDownloader | None = None,
    ) -> None:
        self._crm = crm_gateway
        self._whatsapp = whatsapp_gateway
        self._responses = responses_gateway
        self._transcription = transcription_gateway
        self._file_downloader = file_downloader
        self._sink = sink

    def execute(self, request: RunExecutivePipelineRequest) -> None:
        if request.reset_outputs:
            for path in (
                request.whatsapp_dir,
                request.call_scan_dir,
                request.recordings_dir,
                request.sales_quality_dir,
                request.executive_report_dir,
            ):
                _reset_dir(path)

        deals = self._load_scope_deals(request)
        deal_ids = [str(deal.get("ID") or "") for deal in deals if deal.get("ID")]

        request.executive_report_dir.mkdir(parents=True, exist_ok=True)
        self._sink.write(request.executive_report_dir / "scope-deals.json", deals)

        whatsapp_summary = self._run_whatsapp_steps(request, deals) if request.include_whatsapp else {}
        call_summary = self._run_call_steps(request, deal_ids) if request.include_calls else {}
        sales_quality_summary = self._run_sales_quality_step(request)

        BuildExecutiveReportService(gateway=self._crm, sink=self._sink).execute(
            BuildExecutiveReportRequest(
                output_dir=request.executive_report_dir,
                sales_quality_dir=request.sales_quality_dir,
                scope="bitrix",
                date_from=request.date_from,
                date_to=request.date_to,
                category_ids=request.category_ids,
                responsible_id=(request.responsible_ids or [None])[0],
                responsible_ids=request.responsible_ids,
                deal_ids=request.deal_ids,
                limit=request.limit,
                average_ticket_kzt=request.average_ticket_kzt,
                expected_conversion_pct=request.expected_conversion_pct,
                portal_base_url=request.portal_base_url,
                max_reanimation_cards=request.max_reanimation_cards,
            )
        )

        self._sink.write(
            request.executive_report_dir / "pipeline-summary.json",
            {
                "generated_at": _now_iso(),
                "scope": {
                    "date_from": request.date_from,
                    "date_to": request.date_to,
                    "category_ids": request.category_ids or [],
                    "responsible_ids": request.responsible_ids or [],
                    "deal_ids_count": len(deal_ids),
                    "limit": request.limit,
                },
                "whatsapp": whatsapp_summary,
                "calls": call_summary,
                "sales_quality": sales_quality_summary,
            },
        )

    def _load_scope_deals(self, request: RunExecutivePipelineRequest) -> list[dict[str, Any]]:
        explicit_ids = [str(v).strip() for v in (request.deal_ids or []) if str(v).strip()]
        if explicit_ids:
            rows: list[dict[str, Any]] = []
            for chunk in _chunked(explicit_ids, 50):
                rows.extend(
                    self._crm.list_all(
                        "crm.deal.list",
                        select=_DEAL_SELECT,
                        filter={"ID": chunk if len(chunk) > 1 else chunk[0]},
                        order={"ID": "ASC"},
                        context="executive pipeline explicit deals",
                    )
                )
            return self._filter_scope_rows(rows, request)

        deal_filter: dict[str, Any] = {}
        category_ids = [str(v).strip() for v in (request.category_ids or []) if str(v).strip()]
        if category_ids:
            deal_filter["CATEGORY_ID"] = category_ids if len(category_ids) > 1 else category_ids[0]
        responsible_ids = [
            str(v).strip() for v in (request.responsible_ids or []) if str(v).strip()
        ]
        if responsible_ids:
            deal_filter["ASSIGNED_BY_ID"] = (
                responsible_ids if len(responsible_ids) > 1 else responsible_ids[0]
            )

        rows = self._crm.list_all(
            "crm.deal.list",
            select=_DEAL_SELECT,
            filter=deal_filter,
            order={"DATE_MODIFY": "DESC"},
            context="executive pipeline scope deals",
        )
        rows = self._filter_scope_rows(rows, request)
        return rows[: request.limit] if request.limit > 0 else rows

    def _filter_scope_rows(
        self,
        rows: list[dict[str, Any]],
        request: RunExecutivePipelineRequest,
    ) -> list[dict[str, Any]]:
        filtered = rows
        if request.date_from or request.date_to:
            filtered = [
                row
                for row in filtered
                if within_any_record_datetime_range(
                    row,
                    fields=("DATE_CREATE", "DATE_MODIFY", "CLOSEDATE"),
                    date_from=request.date_from,
                    date_to=request.date_to,
                )
            ]
        category_ids = {str(v).strip() for v in (request.category_ids or []) if str(v).strip()}
        if category_ids:
            filtered = [
                row for row in filtered if str(row.get("CATEGORY_ID") or "") in category_ids
            ]
        responsible_ids = {
            str(v).strip() for v in (request.responsible_ids or []) if str(v).strip()
        }
        if responsible_ids:
            filtered = [
                row
                for row in filtered
                if str(row.get("ASSIGNED_BY_ID") or "") in responsible_ids
            ]
        return filtered

    def _run_whatsapp_steps(
        self,
        request: RunExecutivePipelineRequest,
        deals: list[dict[str, Any]],
    ) -> dict[str, Any]:
        WhatsAppExportService(gateway=self._whatsapp, sink=self._sink).execute(
            WhatsAppExportRequest(
                output_dir=request.whatsapp_dir,
                limit=0,
                deal_rows=deals,
                include_system_messages=True,
            )
        )
        summary = self._conversation_summary(request.whatsapp_dir)
        if summary["rows_with_messages"] == 0 and summary["row_count"] > 0:
            WhatsAppTimelineExportService(gateway=self._whatsapp, sink=self._sink).execute(
                WhatsAppTimelineExportRequest(
                    output_dir=request.whatsapp_dir,
                    limit=0,
                    deal_rows=deals,
                )
            )
            summary = self._conversation_summary(request.whatsapp_dir)

        filtered_count = self._filter_conversations(
            request.whatsapp_dir / "conversations",
            request.whatsapp_dir / "conversations_filtered",
        )
        audio_summary = (
            self._download_and_transcribe_whatsapp_audio(request)
            if request.include_whatsapp_audio
            else {"status": "skipped", "reason": "disabled_for_main_report"}
        )
        return {
            **summary,
            "filtered_conversations": filtered_count,
            "audio": audio_summary,
        }

    def _conversation_summary(self, whatsapp_dir: Path) -> dict[str, int]:
        report_path = whatsapp_dir / "report.json"
        if not report_path.exists():
            return {"row_count": 0, "rows_with_messages": 0}
        report = json.loads(report_path.read_text(encoding="utf-8"))
        rows = report.get("rows") or []
        rows_with_messages = [
            row for row in rows if int(row.get("total_messages") or 0) > 0
        ]
        return {"row_count": len(rows), "rows_with_messages": len(rows_with_messages)}

    def _filter_conversations(self, input_dir: Path, output_dir: Path) -> int:
        _reset_dir(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for path in sorted(input_dir.glob("deal_*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            data["messages"] = [
                message
                for message in data.get("messages", [])
                if isinstance(message, dict)
                and message.get("sender_role") in {"client", "manager"}
            ]
            self._sink.write(output_dir / path.name, data)
            count += 1
        return count

    def _download_and_transcribe_whatsapp_audio(
        self,
        request: RunExecutivePipelineRequest,
    ) -> dict[str, Any]:
        if not self._file_downloader or not self._transcription:
            return {"status": "skipped", "reason": "audio_dependencies_missing"}

        conversations_dir = request.whatsapp_dir / "conversations_filtered"
        audio_base = request.whatsapp_dir / "audio_downloaded"
        audio_base.mkdir(parents=True, exist_ok=True)
        downloaded: list[dict[str, Any]] = []
        download_errors: list[dict[str, Any]] = []

        for conversation_path in sorted(conversations_dir.glob("deal_*.json")):
            data = json.loads(conversation_path.read_text(encoding="utf-8"))
            audio_dir = audio_base / f"{conversation_path.stem}_audio"
            for message_index, message in enumerate(data.get("messages") or []):
                for attachment_index, attachment in enumerate(message.get("attachments") or []):
                    if not isinstance(attachment, dict) or attachment.get("type") != "audio":
                        continue
                    url = str(attachment.get("url") or "").strip()
                    if not url:
                        continue
                    audio_dir.mkdir(parents=True, exist_ok=True)
                    filename = _safe_filename(
                        str(attachment.get("label") or "")
                        or _filename_from_url(url)
                        or f"audio_{message_index}_{attachment_index}.mp3"
                    )
                    destination = _unique_path(audio_dir / filename)
                    try:
                        self._file_downloader.download(
                            self._resolve_download_url(url),
                            destination,
                        )
                        downloaded.append(
                            {
                                "conversation": str(conversation_path),
                                "message_index": message_index,
                                "attachment_index": attachment_index,
                                "file_path": str(destination),
                                "status": "downloaded",
                            }
                        )
                    except Exception as exc:  # noqa: BLE001
                        download_errors.append(
                            {
                                "conversation": str(conversation_path),
                                "message_index": message_index,
                                "attachment_index": attachment_index,
                                "error": str(exc),
                            }
                        )

        transcripts, transcript_errors = self._transcribe_whatsapp_audio(
            conversations_dir=conversations_dir,
            audio_base=audio_base,
            request=request,
        )
        self._sink.write(audio_base / "manifest.json", downloaded)
        self._sink.write(audio_base / "download-errors.json", download_errors)
        self._sink.write(audio_base / "transcripts.json", transcripts)
        self._sink.write(audio_base / "transcript-errors.json", transcript_errors)
        return {
            "downloaded": len(downloaded),
            "download_errors": len(download_errors),
            "transcribed": len(transcripts),
            "transcript_errors": len(transcript_errors),
        }

    def _resolve_download_url(self, original_url: str) -> str:
        file_id = _extract_file_id(original_url)
        if not file_id:
            return original_url
        response = self._whatsapp.call(
            "disk.file.get",
            body={"id": file_id},
            label=f"disk.file.get id={file_id}",
        )
        result = response.get("result") or {}
        if isinstance(result, dict) and result.get("DOWNLOAD_URL"):
            return str(result["DOWNLOAD_URL"])
        return original_url

    def _transcribe_whatsapp_audio(
        self,
        *,
        conversations_dir: Path,
        audio_base: Path,
        request: RunExecutivePipelineRequest,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        transcripts: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for conversation_path in sorted(conversations_dir.glob("deal_*.json")):
            data = json.loads(conversation_path.read_text(encoding="utf-8"))
            changed = False
            audio_dir = audio_base / f"{conversation_path.stem}_audio"
            for message_index, message in enumerate(data.get("messages") or []):
                if str(message.get("text") or "").strip():
                    continue
                for attachment_index, attachment in enumerate(message.get("attachments") or []):
                    if not isinstance(attachment, dict) or attachment.get("type") != "audio":
                        continue
                    label = str(attachment.get("label") or "").strip()
                    audio_file = _find_audio_file(audio_dir, label)
                    if audio_file is None:
                        continue
                    try:
                        result = self._transcription.transcribe(
                            file_path=audio_file,
                            model=request.transcription_model,
                            language=request.transcription_language,
                            prompt=request.transcription_prompt,
                        )
                        text = str(result.get("text") or "").strip()
                        if not text:
                            continue
                        message["text"] = text
                        attachment["transcribed"] = True
                        attachment["transcript"] = text
                        changed = True
                        transcripts.append(
                            {
                                "conversation": str(conversation_path),
                                "message_index": message_index,
                                "attachment_index": attachment_index,
                                "audio_file": str(audio_file),
                                "text_length": len(text),
                            }
                        )
                    except Exception as exc:  # noqa: BLE001
                        errors.append(
                            {
                                "conversation": str(conversation_path),
                                "message_index": message_index,
                                "attachment_index": attachment_index,
                                "audio_file": str(audio_file),
                                "error": str(exc),
                            }
                        )
            if changed:
                self._sink.write(conversation_path, data)
        return transcripts, errors

    def _run_call_steps(
        self,
        request: RunExecutivePipelineRequest,
        deal_ids: list[str],
    ) -> dict[str, Any]:
        if not self._file_downloader or not self._transcription:
            return {"status": "skipped", "reason": "call_dependencies_missing"}
        if not deal_ids:
            return {"recording_candidates": 0, "downloaded": 0, "transcribed": 0}

        CallRecordsScanService(gateway=self._crm, sink=self._sink).execute(
            CallRecordsScanRequest(
                output_dir=request.call_scan_dir,
                limit=0,
                date_from=request.date_from,
                date_to=request.date_to,
                deal_ids=deal_ids,
            )
        )
        candidate_count = _json_list_count(request.call_scan_dir / "recording-candidates.json")
        if candidate_count == 0:
            return {"recording_candidates": 0, "downloaded": 0, "transcribed": 0}

        DownloadRecordingsService(downloader=self._file_downloader, sink=self._sink).execute(
            DownloadRecordingsRequest(
                source_json_path=request.call_scan_dir / "recording-candidates.json",
                output_dir=request.recordings_dir,
                skip_existing=not request.reset_outputs,
            )
        )
        manifest_count = _json_list_count(request.recordings_dir / "manifest.json")
        if manifest_count == 0:
            return {
                "recording_candidates": candidate_count,
                "downloaded": 0,
                "transcribed": 0,
            }

        transcripts_dir = request.recordings_dir / "transcripts"
        TranscribeRecordingsService(gateway=self._transcription, sink=self._sink).execute(
            TranscribeRecordingsRequest(
                manifest_path=request.recordings_dir / "manifest.json",
                output_dir=transcripts_dir,
                model=request.transcription_model,
                language=request.transcription_language,
                prompt=request.transcription_prompt,
                limit=0,
                skip_existing=not request.reset_outputs,
            )
        )
        return {
            "recording_candidates": candidate_count,
            "downloaded": manifest_count,
            "transcribed": _json_list_count(transcripts_dir / "manifest.json"),
        }

    def _run_sales_quality_step(self, request: RunExecutivePipelineRequest) -> dict[str, Any]:
        call_manifest = request.recordings_dir / "transcripts" / "manifest.json"
        whatsapp_dir = request.whatsapp_dir / "conversations_filtered"
        try:
            AnalyzeSalesQualityService(gateway=self._responses, sink=self._sink).execute(
                AnalyzeSalesQualityRequest(
                    output_dir=request.sales_quality_dir,
                    call_transcript_manifest_path=call_manifest if call_manifest.exists() else None,
                    call_metadata_path=request.call_scan_dir / "recording-candidates.json"
                    if (request.call_scan_dir / "recording-candidates.json").exists()
                    else None,
                    activity_metadata_path=request.call_scan_dir / "activities.source.json"
                    if (request.call_scan_dir / "activities.source.json").exists()
                    else None,
                    whatsapp_conversation_dir=whatsapp_dir if whatsapp_dir.exists() else None,
                    model=request.model,
                    limit=0,
                    skip_existing=not request.reset_outputs,
                    slow_response_threshold_sec=request.slow_response_threshold_sec,
                    max_chars_per_item=request.max_chars_per_item,
                )
            )
        except ValueError as exc:
            if "No call transcripts or WhatsApp conversations found" not in str(exc):
                raise
            self._write_empty_sales_quality(request.sales_quality_dir)

        return {
            "features": len(list((request.sales_quality_dir / "features").glob("*.json"))),
            "errors": _json_list_count(request.sales_quality_dir / "errors.json"),
        }

    def _write_empty_sales_quality(self, output_dir: Path) -> None:
        (output_dir / "features").mkdir(parents=True, exist_ok=True)
        (output_dir / "raw").mkdir(parents=True, exist_ok=True)
        report = {
            "generated_at": _now_iso(),
            "total_interactions": 0,
            "overall_stage_score_pct": None,
            "stage_funnel": [],
            "top_problems": [],
            "per_manager": [],
            "visuals": {"manager_heatmap": []},
        }
        self._sink.write(output_dir / "report.json", report)
        self._sink.write(output_dir / "errors.json", [])
        self._sink.write(output_dir / "usage-events.json", [])
        self._sink.write(output_dir / "usage-summary.json", {})


def _reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def _json_list_count(path: Path) -> int:
    if not path.exists():
        return 0
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], list):
        raw = raw[0]
    if not isinstance(raw, list):
        raw = [raw]
    return len([item for item in raw if isinstance(item, dict)])


def _extract_file_id(url: str) -> str | None:
    qs = parse_qs(urlparse(url).query)
    ids = qs.get("fileId") or qs.get("id")
    return ids[0] if ids else None


def _filename_from_url(url: str) -> str:
    qs = parse_qs(urlparse(url).query)
    if qs.get("fileName"):
        return str(qs["fileName"][0])
    name = Path(urlparse(url).path).name
    return name or ""


def _safe_filename(value: str) -> str:
    safe = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value).strip()
    return safe or "audio.mp3"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    index = 1
    while True:
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _find_audio_file(audio_dir: Path, label: str) -> Path | None:
    if not audio_dir.exists():
        return None
    if label:
        candidate = audio_dir / _safe_filename(label)
        if candidate.exists():
            return candidate
        label_stem = Path(label).stem.lower()
        for path in audio_dir.iterdir():
            if path.is_file() and path.stem.lower() == label_stem:
                return path
    files = [path for path in audio_dir.iterdir() if path.is_file()]
    return files[0] if len(files) == 1 else None


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
