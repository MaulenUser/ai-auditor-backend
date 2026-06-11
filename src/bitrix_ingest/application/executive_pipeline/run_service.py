"""Run the executive report pipeline with one shared CRM scope."""
from __future__ import annotations

import hashlib
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
from ..date_range import build_closed_filter, within_any_record_datetime_range
from ..executive_report import BuildExecutiveReportRequest, BuildExecutiveReportService
from ..ports import BitrixGateway, FileDownloader, JsonSink
from ..progress import ProgressCallback, emit_progress
from ..recordings import DownloadRecordingsRequest, DownloadRecordingsService
from ..sales_quality import AnalyzeSalesQualityRequest, AnalyzeSalesQualityService
from ..transcribe import TranscribeRecordingsRequest, TranscribeRecordingsService
from ..whatsapp import WhatsAppExportRequest, WhatsAppExportService
from ..whatsapp_timeline import WhatsAppTimelineExportRequest, WhatsAppTimelineExportService

logger = logging.getLogger(__name__)

_DEAL_DATE_FILTER = "DATE_CREATE"

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
class _ScopeDealsResult:
    rows: list[dict[str, Any]]
    reused_cache: bool


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
    portal_base_url: str = ""
    max_reanimation_cards: int = 100
    include_whatsapp: bool = True
    include_whatsapp_audio: bool = False
    include_calls: bool = True
    reset_outputs: bool = True
    progress_callback: ProgressCallback | None = None


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
        _report_stage(
            request.progress_callback,
            stage="preparing",
            label="Подготовка отчёта",
            start=0,
            end=4,
            current=0,
            total=1,
            message="Готовим папки и параметры запуска",
        )
        if request.reset_outputs:
            for path in (
                request.whatsapp_dir,
                request.call_scan_dir,
                request.recordings_dir,
                request.sales_quality_dir,
                request.executive_report_dir,
            ):
                _reset_dir(path)

        _report_stage(
            request.progress_callback,
            stage="crm_scope",
            label="Загрузка сделок",
            start=4,
            end=10,
            current=0,
            total=1,
            message="Загружаем сделки по DATE_CREATE за выбранный период",
        )
        scope_result = self._load_scope_deals(request)
        deals = scope_result.rows
        deal_ids = [str(deal.get("ID") or "") for deal in deals if deal.get("ID")]

        if not request.reset_outputs and not scope_result.reused_cache:
            _reset_dir(request.whatsapp_dir)
            _reset_dir(request.call_scan_dir)

        request.executive_report_dir.mkdir(parents=True, exist_ok=True)
        self._sink.write(request.executive_report_dir / "scope-deals.json", deals)
        _report_stage(
            request.progress_callback,
            stage="crm_scope",
            label="Загрузка сделок",
            start=4,
            end=10,
            current=1,
            total=1,
            message=f"Сделки в AI-scope: {len(deal_ids)}",
        )

        _report_stage(
            request.progress_callback,
            stage="whatsapp",
            label="Загрузка переписок",
            start=10,
            end=18,
            current=0,
            total=1,
            message="Загружаем переписки по сделкам из AI-scope",
        )
        whatsapp_summary = self._run_whatsapp_steps(request, deals) if request.include_whatsapp else {}
        _report_stage(
            request.progress_callback,
            stage="whatsapp",
            label="Загрузка переписок",
            start=10,
            end=18,
            current=1,
            total=1,
            message=(
                "Переписки загружены: "
                f"{whatsapp_summary.get('rows_with_messages', 0)} с сообщениями"
            ),
        )
        call_summary = self._run_call_steps(request, deal_ids) if request.include_calls else {}
        _report_stage(
            request.progress_callback,
            stage="sales_quality",
            label="AI-оценка коммуникаций",
            start=62,
            end=88,
            current=0,
            total=1,
            message="Готовим коммуникации к AI-оценке",
        )
        sales_quality_summary = self._run_sales_quality_step(request)

        _report_stage(
            request.progress_callback,
            stage="executive_report",
            label="Сборка AI-отчёта",
            start=88,
            end=92,
            current=0,
            total=1,
            message="Собираем AI-часть отчёта",
        )
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
        _report_stage(
            request.progress_callback,
            stage="executive_report",
            label="Сборка AI-отчёта",
            start=88,
            end=92,
            current=1,
            total=1,
            message="AI-часть отчёта собрана",
        )

        self._sink.write(
            request.executive_report_dir / "pipeline-summary.json",
            {
                "generated_at": _now_iso(),
                "scope": {
                    "deal_date_filter": _DEAL_DATE_FILTER,
                    "date_from": request.date_from,
                    "date_to": request.date_to,
                    "category_ids": request.category_ids or [],
                    "responsible_ids": request.responsible_ids or [],
                    "deal_ids_count": len(deal_ids),
                    "requested_deal_ids_count": len(_normalize_values(request.deal_ids or [])),
                    "deal_ids_fingerprint": _fingerprint_values(request.deal_ids or []),
                    "limit": request.limit,
                },
                "whatsapp": whatsapp_summary,
                "calls": call_summary,
                "sales_quality": sales_quality_summary,
            },
        )

    def _load_scope_deals(self, request: RunExecutivePipelineRequest) -> _ScopeDealsResult:
        if not request.reset_outputs:
            cached_rows = _json_dict_list(request.executive_report_dir / "scope-deals.json")
            if cached_rows is not None and _scope_cache_matches_request(request):
                return _ScopeDealsResult(cached_rows, reused_cache=True)

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
            return _ScopeDealsResult(self._filter_scope_rows(rows, request), reused_cache=False)

        deal_filter: dict[str, Any] = {}
        deal_filter.update(
            build_closed_filter(
                _DEAL_DATE_FILTER,
                date_from=request.date_from,
                date_to=request.date_to,
            )
        )
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
            order={_DEAL_DATE_FILTER: "DESC"},
            context="executive pipeline scope deals",
            limit=request.limit if request.limit > 0 else None,
        )
        rows = self._filter_scope_rows(rows, request)
        if request.limit > 0:
            rows = rows[: request.limit]
        return _ScopeDealsResult(rows, reused_cache=False)

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
                    fields=(_DEAL_DATE_FILTER,),
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
        if not request.reset_outputs:
            cached = self._cached_whatsapp_summary(request)
            if cached is not None:
                return cached

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

    def _cached_whatsapp_summary(
        self,
        request: RunExecutivePipelineRequest,
    ) -> dict[str, Any] | None:
        if not (request.whatsapp_dir / "report.json").exists():
            return None
        filtered_dir = request.whatsapp_dir / "conversations_filtered"
        if not filtered_dir.exists():
            conversations_dir = request.whatsapp_dir / "conversations"
            if not conversations_dir.exists():
                return None
            self._filter_conversations(conversations_dir, filtered_dir)

        summary = self._conversation_summary(request.whatsapp_dir)
        filtered_count = _json_file_count(filtered_dir)
        if summary["rows_with_messages"] > 0 and filtered_count == 0:
            return None
        audio_summary = (
            self._download_and_transcribe_whatsapp_audio(request)
            if request.include_whatsapp_audio
            else {"status": "skipped", "reason": "resume_existing_outputs"}
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
            _report_stage(
                request.progress_callback,
                stage="call_scan",
                label="Поиск звонков",
                start=18,
                end=62,
                current=1,
                total=1,
                message="Звонки пропущены: нет зависимостей для скачивания или транскрибации",
            )
            return {"status": "skipped", "reason": "call_dependencies_missing"}
        if not deal_ids:
            _report_stage(
                request.progress_callback,
                stage="call_scan",
                label="Поиск звонков",
                start=18,
                end=62,
                current=1,
                total=1,
                message="Звонки не найдены: в AI-scope нет сделок",
            )
            return {"recording_candidates": 0, "downloaded": 0, "transcribed": 0}

        candidates_path = request.call_scan_dir / "recording-candidates.json"
        if request.reset_outputs or not candidates_path.exists():
            CallRecordsScanService(gateway=self._crm, sink=self._sink).execute(
                CallRecordsScanRequest(
                    output_dir=request.call_scan_dir,
                    limit=0,
                    date_from=request.date_from,
                    date_to=request.date_to,
                    deal_ids=deal_ids,
                    progress_callback=_stage_callback(
                        request.progress_callback,
                        stage="call_scan",
                        label="Поиск звонков",
                        start=18,
                        end=30,
                    ),
                )
            )
        else:
            _report_stage(
                request.progress_callback,
                stage="call_scan",
                label="Поиск звонков",
                start=18,
                end=30,
                current=1,
                total=1,
                message="Используем готовый список звонков",
            )
        candidate_count = _json_list_count(candidates_path)
        if candidate_count == 0:
            _report_stage(
                request.progress_callback,
                stage="download_recordings",
                label="Скачивание записей",
                start=30,
                end=62,
                current=1,
                total=1,
                message="Записей звонков для анализа нет",
            )
            return {"recording_candidates": 0, "downloaded": 0, "transcribed": 0}

        DownloadRecordingsService(downloader=self._file_downloader, sink=self._sink).execute(
            DownloadRecordingsRequest(
                source_json_path=request.call_scan_dir / "recording-candidates.json",
                output_dir=request.recordings_dir,
                skip_existing=not request.reset_outputs,
                progress_callback=_stage_callback(
                    request.progress_callback,
                    stage="download_recordings",
                    label="Скачивание записей",
                    start=30,
                    end=38,
                ),
            )
        )
        manifest_count = _json_list_count(request.recordings_dir / "manifest.json")
        if manifest_count == 0:
            _report_stage(
                request.progress_callback,
                stage="transcription",
                label="Транскрибация звонков",
                start=38,
                end=62,
                current=1,
                total=1,
                message="Скачанных записей для транскрибации нет",
            )
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
                progress_callback=_stage_callback(
                    request.progress_callback,
                    stage="transcription",
                    label="Транскрибация звонков",
                    start=38,
                    end=62,
                ),
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
                    progress_callback=_stage_callback(
                        request.progress_callback,
                        stage="sales_quality",
                        label="AI-оценка коммуникаций",
                        start=62,
                        end=88,
                    ),
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


def _stage_callback(
    callback: ProgressCallback | None,
    *,
    stage: str,
    label: str,
    start: float,
    end: float,
) -> ProgressCallback | None:
    if callback is None:
        return None

    def _callback(event: dict[str, Any]) -> None:
        _report_stage(
            callback,
            stage=stage,
            label=label,
            start=start,
            end=end,
            current=_coerce_int(event.get("current")),
            total=_coerce_int(event.get("total")),
            message=str(event.get("message") or label),
        )

    return _callback


def _report_stage(
    callback: ProgressCallback | None,
    *,
    stage: str,
    label: str,
    start: float,
    end: float,
    current: int,
    total: int,
    message: str,
) -> None:
    if total > 0:
        ratio = max(0.0, min(1.0, current / total))
        percent = start + ((end - start) * ratio)
    else:
        percent = start
    emit_progress(
        callback,
        stage=stage,
        stage_label=label,
        current=max(0, current),
        total=max(0, total),
        percent=round(percent, 1),
        message=message,
    )


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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


def _json_dict_list(path: Path) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return None
    return [item for item in raw if isinstance(item, dict)]


def _scope_cache_matches_request(request: RunExecutivePipelineRequest) -> bool:
    summary_path = request.executive_report_dir / "pipeline-summary.json"
    if not summary_path.exists():
        return False
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    scope = summary.get("scope") if isinstance(summary, dict) else None
    if not isinstance(scope, dict):
        return False
    if scope.get("deal_date_filter") != _DEAL_DATE_FILTER:
        return False
    return (
        scope.get("date_from") == request.date_from
        and scope.get("date_to") == request.date_to
        and _normalize_values(scope.get("category_ids") or [])
        == _normalize_values(request.category_ids or [])
        and _normalize_values(scope.get("responsible_ids") or [])
        == _normalize_values(request.responsible_ids or [])
        and scope.get("deal_ids_fingerprint") == _fingerprint_values(request.deal_ids or [])
        and int(scope.get("limit") or 0) == int(request.limit or 0)
    )


def _normalize_values(values: list[Any]) -> list[str]:
    return sorted(str(value).strip() for value in values if str(value).strip())


def _fingerprint_values(values: list[Any]) -> str:
    normalized = _normalize_values(values)
    payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _json_file_count(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    return len([item for item in path.glob("*.json") if item.is_file()])


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
