"""FastAPI application exposing the Bitrix exporters and OpenAI pipelines as HTTP endpoints."""
from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from fastapi import FastAPI, Form, HTTPException, Query, Security
from fastapi.security import APIKeyHeader

from ..application.analytics import (
    AggregateFeatureRequest,
    AggregateFeatureService,
    GenerateRecommendationsRequest,
    GenerateRecommendationsService,
)
from ..application.audit import RunAuditRequest, RunAuditService
from ..application.call_features import ExtractCallFeaturesRequest, ExtractCallFeaturesService
from ..application.call_records import CallRecordsScanRequest, CallRecordsScanService
from ..application.catalog import GetCatalogService
from ..application.crm import CrmExportRequest, CrmExportService
from ..application.recordings import DownloadRecordingsRequest, DownloadRecordingsService
from ..application.transcribe import TranscribeRecordingsRequest, TranscribeRecordingsService
from ..application.whatsapp import WhatsAppExportRequest, WhatsAppExportService
from ..application.whatsapp_features import (
    ExtractWhatsAppFeaturesRequest,
    ExtractWhatsAppFeaturesService,
)
from ..application.whatsapp_timeline import (
    WhatsAppTimelineExportRequest,
    WhatsAppTimelineExportService,
)
from ..domain.exceptions import DomainError
from ..infrastructure.http import BitrixClient
from ..infrastructure.http.file_downloader import RequestsFileDownloader
from ..infrastructure.openai import OpenAiResponsesClient, OpenAiTranscriptionClient
from ..infrastructure.persistence import FileSystemJsonWriter
from ..infrastructure.persistence.memory_writer import InMemoryJsonSink, TeeJsonSink

# ---------------------------------------------------------------------------
# Security schemes — shown in the Swagger "Authorize" dialog
# ---------------------------------------------------------------------------

_webhook_header = APIKeyHeader(
    name="X-Webhook-Url",
    scheme_name="WebhookUrl",
    description="Bitrix24 webhook URL для CRM и записей звонков",
    auto_error=False,
)

_whatsapp_webhook_header = APIKeyHeader(
    name="X-Whatsapp-Webhook-Url",
    scheme_name="WhatsappWebhookUrl",
    description="Bitrix24 webhook URL для WhatsApp-экспорта (можно отдельный)",
    auto_error=False,
)

_openai_key_header = APIKeyHeader(
    name="X-OpenAI-Api-Key",
    scheme_name="OpenAIApiKey",
    description="OpenAI API key для транскрипции и извлечения фич (sk-...)",
    auto_error=False,
)

# ---------------------------------------------------------------------------

app = FastAPI(
    title="Bitrix Ingest API",
    version="0.1.0",
    description=(
        "Bitrix24 экспорт и OpenAI-пайплайн в одном интерфейсе.\n\n"
        "Нажмите **Authorize** и укажите:\n"
        "- `X-Webhook-Url` — для CRM / звонков\n"
        "- `X-Whatsapp-Webhook-Url` — для WhatsApp\n"
        "- `X-OpenAI-Api-Key` — для транскрипции и извлечения фич"
    ),
    swagger_ui_parameters={"persistAuthorization": True},
)

# Default output directories matching CLI defaults.
_CRM_DIR = Path("export")
_CALLS_DIR = Path("export/call-records-scan")
_WA_DIR = Path("export/whatsapp-timeline")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _none(value: str | None) -> str | None:
    """Treat form strings 'null', 'none', '' as None."""
    if not value or value.strip().lower() in ("null", "none"):
        return None
    return value


def _require_webhook(header_url: str | None) -> str:
    if not header_url:
        raise HTTPException(status_code=422, detail="Укажите X-Webhook-Url в Authorize.")
    return header_url


def _require_openai_key(header_key: str | None) -> str:
    if not header_key:
        raise HTTPException(status_code=422, detail="Укажите X-OpenAI-Api-Key в Authorize.")
    return header_key


def _tee() -> tuple[TeeJsonSink, InMemoryJsonSink]:
    mem = InMemoryJsonSink()
    return TeeJsonSink(primary=FileSystemJsonWriter(), secondary=mem), mem


def _run_service(fn: Any, mem: InMemoryJsonSink) -> dict[str, Any]:
    try:
        fn()
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DomainError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "ok", "data": mem.data}


# ---------------------------------------------------------------------------
# Bitrix endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Catalog endpoints — for UI dropdowns (funnels, managers)
# ---------------------------------------------------------------------------


@app.get(
    "/catalog/funnels",
    summary="Список воронок продаж",
    tags=["Catalog"],
)
def get_funnels(
    webhook_url: str | None = Security(_webhook_header),
) -> dict[str, Any]:
    """Возвращает список воронок (crm.dealcategory.list) для выбора в UI."""
    url = _require_webhook(webhook_url)
    try:
        funnels = GetCatalogService(gateway=BitrixClient(url)).get_funnels()
    except DomainError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"funnels": funnels}


@app.get(
    "/catalog/managers",
    summary="Список менеджеров",
    tags=["Catalog"],
)
def get_managers(
    webhook_url: str | None = Security(_webhook_header),
) -> dict[str, Any]:
    """Возвращает список пользователей портала (user.get) для выбора менеджера."""
    url = _require_webhook(webhook_url)
    try:
        managers = GetCatalogService(gateway=BitrixClient(url)).get_managers()
    except DomainError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"managers": managers}


@app.get(
    "/audit/preview",
    summary="Предварительный расчёт аудита",
    tags=["Audit"],
)
def preview_audit(
    funnel_id: Optional[List[str]] = Query(None, description="ID воронок (можно несколько: ?funnel_id=2&funnel_id=4)"),
    date_from: Optional[str] = Query(None, description="Дата начала (ISO 8601)"),
    date_to: Optional[str] = Query(None, description="Дата конца (ISO 8601)"),
    responsible_id: Optional[str] = Query(None, description="ID менеджера (пусто = весь отдел)"),
    webhook_url: str | None = Security(_whatsapp_webhook_header),
) -> dict[str, Any]:
    """Считает сколько обращений, сотрудников и сделок попадёт в аудит **без** запуска экспорта.

    Поддерживает несколько воронок: ``?funnel_id=2&funnel_id=4&funnel_id=6``

    Возвращает:
    - ``deal_count`` — WhatsApp-сделки в выборке
    - ``manager_count`` / ``managers`` — сотрудники, попавшие в выборку (с именами)
    - ``funnel_count`` / ``funnels`` — выбранные воронки
    - ``period_from`` / ``period_to`` — запрошенный период
    - ``actual_date_from`` / ``actual_date_to`` — фактический диапазон в данных
    - ``total_deals_scanned`` — всего сделок просмотрено до WhatsApp-фильтра
    """
    url = _require_webhook(webhook_url)
    try:
        preview = GetCatalogService(gateway=BitrixClient(url)).get_audit_preview(
            funnel_ids=funnel_id or None,
            date_from=_none(date_from),
            date_to=_none(date_to),
            responsible_id=_none(responsible_id),
        )
    except DomainError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return preview


# ---------------------------------------------------------------------------
# AI Audit — full pipeline in one call
# ---------------------------------------------------------------------------


@app.post(
    "/audit/run",
    summary="Запуск AI аудита",
    tags=["Audit"],
)
def run_audit(
    funnel_id: Optional[List[str]] = Form(None, description="ID воронок (можно несколько полей с одним именем)"),
    date_from: Optional[str] = Form(None, description="Дата начала анализа (ISO 8601)"),
    date_to: Optional[str] = Form(None, description="Дата конца анализа (ISO 8601)"),
    responsible_id: Optional[str] = Form(None, description="ID менеджера (пусто = весь отдел)"),
    limit: int = Form(0, description="Лимит сделок (0 = все)"),
    model: str = Form("gpt-4o-mini", description="OpenAI модель для извлечения фич"),
    recommendations_model: str = Form("gpt-4o", description="OpenAI модель для рекомендаций"),
    source_label: Optional[str] = Form(None, description="Метка источника в отчёте"),
    output_dir: str = Form("export/audit", description="Базовая папка вывода"),
    webhook_url: str | None = Security(_whatsapp_webhook_header),
    openai_key: str | None = Security(_openai_key_header),
) -> dict[str, Any]:
    """Полный AI-аудит: WhatsApp-переписки → фичи → агрегат → рекомендации.

    Поддерживает несколько воронок одновременно (передай ``funnel_id`` несколько раз).

    Шаги:
    1. Экспорт WhatsApp-переписок по выбранным воронкам / периоду / менеджеру
    2. Извлечение фич по каждой переписке (OpenAI)
    3. Агрегация фич по отделу
    4. Генерация рекомендаций (OpenAI)

    Результаты сохраняются в ``output_dir/whatsapp-timeline/``, ``output_dir/whatsapp-features/``,
    ``output_dir/analytics/``.
    """
    bitrix_url = _require_webhook(webhook_url)
    key = _require_openai_key(openai_key)

    clean_funnels = [f for f in (funnel_id or []) if _none(f)] or None
    resolved_responsible = _none(responsible_id)

    funnel_slug = ("funnels_" + "_".join(clean_funnels)) if clean_funnels else "all_funnels"
    resolved_output = Path(output_dir) / funnel_slug

    sink, mem = _tee()
    return _run_service(
        lambda: RunAuditService(
            bitrix_gateway=BitrixClient(bitrix_url),
            responses_gateway=OpenAiResponsesClient(key),
            sink=sink,
        ).execute(
            RunAuditRequest(
                output_dir=resolved_output,
                funnel_ids=clean_funnels,
                date_from=_none(date_from),
                date_to=_none(date_to),
                responsible_id=resolved_responsible,
                limit=limit,
                model=model,
                recommendations_model=recommendations_model,
                source_label=_none(source_label) or "",
            )
        ),
        mem,
    )


@app.post("/crm/export")
def export_crm(
    date_from: Optional[str] = Form(None, description="Дата начала (ISO 8601)"),
    date_to: Optional[str] = Form(None, description="Дата конца (ISO 8601)"),
    skip_users: bool = Form(False, description="Пропустить экспорт пользователей"),
    skip_activities: bool = Form(False, description="Пропустить экспорт активностей"),
    limit: Optional[int] = Form(None, description="Максимальное число сделок (пусто = все)"),
    webhook_url: str | None = Security(_webhook_header),
) -> dict[str, Any]:
    """Экспорт CRM-снапшота → ``export/``."""
    url = _require_webhook(webhook_url)
    sink, mem = _tee()
    return _run_service(
        lambda: CrmExportService(gateway=BitrixClient(url), sink=sink).execute(
            CrmExportRequest(
                output_dir=_CRM_DIR,
                date_from=_none(date_from), date_to=_none(date_to),
                skip_users=skip_users, skip_activities=skip_activities,
                limit=limit,
            )
        ),
        mem,
    )


@app.post("/call-records/scan")
def scan_call_records(
    date_from: Optional[str] = Form(None, description="Дата начала (ISO 8601)"),
    date_to: Optional[str] = Form(None, description="Дата конца (ISO 8601)"),
    limit: int = Form(20, description="Максимальное число записей"),
    responsible_id: Optional[str] = Form(None, description="ID менеджера (пусто = все)"),
    webhook_url: str | None = Security(_webhook_header),
) -> dict[str, Any]:
    """Сканирование записей звонков → ``export/call-records-scan/``."""
    url = _require_webhook(webhook_url)
    sink, mem = _tee()
    return _run_service(
        lambda: CallRecordsScanService(gateway=BitrixClient(url), sink=sink).execute(
            CallRecordsScanRequest(
                output_dir=_CALLS_DIR, limit=limit,
                date_from=_none(date_from), date_to=_none(date_to),
                responsible_id=_none(responsible_id),
            )
        ),
        mem,
    )


@app.post("/recordings/download")
def download_recordings(
    source_json: str = Form(
        "export/call-records-scan/recording-candidates.json",
        description="Путь к recording-candidates.json",
    ),
    output_dir: str = Form("export/recordings", description="Папка для аудиофайлов"),
    skip_existing: bool = Form(False, description="Пропускать уже скачанные файлы"),
) -> dict[str, Any]:
    """Скачивание записей звонков на диск → ``export/recordings/``."""
    sink, mem = _tee()
    return _run_service(
        lambda: DownloadRecordingsService(
            downloader=RequestsFileDownloader(), sink=sink,
        ).execute(
            DownloadRecordingsRequest(
                source_json_path=Path(source_json),
                output_dir=Path(output_dir),
                skip_existing=skip_existing,
            )
        ),
        mem,
    )


@app.post("/whatsapp/export")
def export_whatsapp(
    date_from: Optional[str] = Form(None, description="Дата начала (ISO 8601)"),
    date_to: Optional[str] = Form(None, description="Дата конца (ISO 8601)"),
    limit: int = Form(100, description="Максимальное число сделок"),
    deal_ids: Optional[List[str]] = Form(None, description="ID сделок (напр. 51056)"),
    funnel_id: Optional[List[str]] = Form(None, description="ID воронок (можно несколько)"),
    responsible_id: Optional[str] = Form(None, description="ID менеджера (ASSIGNED_BY_ID)"),
    exclude_system_messages: bool = Form(False, description="Исключить системные сообщения"),
    page_delay: float = Form(0.3, description="Пауза между страницами (сек)"),
    webhook_url: str | None = Security(_whatsapp_webhook_header),
) -> dict[str, Any]:
    """Экспорт WhatsApp через Open Lines API → ``export/whatsapp-timeline/``."""
    url = _require_webhook(webhook_url)
    clean_funnels = [f for f in (funnel_id or []) if _none(f)] or None
    sink, mem = _tee()
    return _run_service(
        lambda: WhatsAppExportService(
            gateway=BitrixClient(url, page_delay=page_delay), sink=sink,
        ).execute(
            WhatsAppExportRequest(
                output_dir=_WA_DIR, limit=limit,
                date_from=_none(date_from), date_to=_none(date_to),
                deal_ids=deal_ids, skip_existing=False,
                include_system_messages=not exclude_system_messages,
                category_ids=clean_funnels,
                responsible_id=_none(responsible_id),
            )
        ),
        mem,
    )


@app.post("/whatsapp-timeline/export")
def export_whatsapp_timeline(
    date_from: Optional[str] = Form(None, description="Дата начала (ISO 8601)"),
    date_to: Optional[str] = Form(None, description="Дата конца (ISO 8601)"),
    limit: int = Form(100, description="Максимальное число сделок"),
    deal_ids: Optional[List[str]] = Form(None, description="ID сделок (напр. 51056)"),
    funnel_id: Optional[List[str]] = Form(None, description="ID воронок (можно несколько)"),
    responsible_id: Optional[str] = Form(None, description="ID менеджера (ASSIGNED_BY_ID)"),
    skip_existing: bool = Form(False, description="Пропускать уже экспортированные сделки"),
    page_delay: float = Form(0.0, description="Пауза между страницами (сек)"),
    output_dir: str = Form("export/whatsapp-timeline", description="Папка вывода"),
    webhook_url: str | None = Security(_whatsapp_webhook_header),
) -> dict[str, Any]:
    """Экспорт WhatsApp через timeline-комментарии (Wazzup-маркеры) → ``export/whatsapp-timeline/``."""
    url = _require_webhook(webhook_url)
    clean_funnels = [f for f in (funnel_id or []) if _none(f)] or None
    sink, mem = _tee()
    return _run_service(
        lambda: WhatsAppTimelineExportService(
            gateway=BitrixClient(url, page_delay=page_delay), sink=sink,
        ).execute(
            WhatsAppTimelineExportRequest(
                output_dir=Path(output_dir), limit=limit,
                date_from=_none(date_from), date_to=_none(date_to),
                deal_ids=deal_ids, skip_existing=skip_existing,
                category_ids=clean_funnels,
                responsible_id=_none(responsible_id),
            )
        ),
        mem,
    )


# ---------------------------------------------------------------------------
# OpenAI pipeline endpoints
# ---------------------------------------------------------------------------


@app.post("/transcribe/recordings")
def transcribe_recordings(
    manifest_path: str = Form(
        "export/recordings/manifest.json",
        description="Путь к manifest.json из /recordings/download",
    ),
    output_dir: str = Form("export/transcripts", description="Папка для транскриптов"),
    model: str = Form("gpt-4o-transcribe", description="Модель Whisper"),
    language: Optional[str] = Form(None, description="Код языка, напр. ru"),
    prompt: Optional[str] = Form(None, description="Подсказка для транскрипции"),
    limit: int = Form(0, description="Максимум файлов (0 = все)"),
    skip_existing: bool = Form(False, description="Пропускать уже транскрибированные"),
    openai_key: str | None = Security(_openai_key_header),
) -> dict[str, Any]:
    """Транскрипция аудиозаписей через OpenAI Whisper → ``export/transcripts/``."""
    key = _require_openai_key(openai_key)
    sink, mem = _tee()
    return _run_service(
        lambda: TranscribeRecordingsService(
            gateway=OpenAiTranscriptionClient(key), sink=sink,
        ).execute(
            TranscribeRecordingsRequest(
                manifest_path=Path(manifest_path),
                output_dir=Path(output_dir),
                model=model, language=language, prompt=prompt,
                limit=limit, skip_existing=skip_existing,
            )
        ),
        mem,
    )


@app.post("/call-features/extract")
def extract_call_features(
    transcript_manifest: str = Form(
        "export/transcripts/manifest.json",
        description="Путь к manifest.json из /transcribe/recordings",
    ),
    call_metadata: str = Form(
        "export/call-records-scan/recording-candidates.json",
        description="Путь к recording-candidates.json (метаданные звонков)",
    ),
    activity_metadata: str = Form(
        "export/call-records-scan/activities.source.json",
        description="Путь к activities.source.json (направление звонка)",
    ),
    output_dir: str = Form("export/call-features", description="Папка для фич"),
    model: str = Form("gpt-4o-mini", description="OpenAI модель"),
    limit: int = Form(0, description="Максимум записей (0 = все)"),
    skip_existing: bool = Form(False, description="Пропускать уже обработанные"),
    openai_key: str | None = Security(_openai_key_header),
) -> dict[str, Any]:
    """Извлечение фич из транскриптов звонков через OpenAI → ``export/call-features/``."""
    key = _require_openai_key(openai_key)
    sink, mem = _tee()
    return _run_service(
        lambda: ExtractCallFeaturesService(
            gateway=OpenAiResponsesClient(key), sink=sink,
        ).execute(
            ExtractCallFeaturesRequest(
                transcript_manifest_path=Path(transcript_manifest),
                output_dir=Path(output_dir),
                call_metadata_path=Path(call_metadata) if call_metadata else None,
                activity_metadata_path=Path(activity_metadata) if activity_metadata else None,
                model=model, limit=limit, skip_existing=skip_existing,
            )
        ),
        mem,
    )


@app.post("/analytics/aggregate")
def aggregate_features(
    features_dir: str = Form(
        "export/call-features/features",
        description="Папка с feature JSON-файлами (звонки или WhatsApp)",
    ),
    output_dir: str = Form(
        "export/call-analytics",
        description="Папка для агрегата",
    ),
    limit: int = Form(0, description="Максимум файлов (0 = все)"),
) -> dict[str, Any]:
    """Агрегация feature-файлов в статистику по отделу продаж.

    Считает распределения outcome, qualification, sales_process, теги, статистику по менеджерам.
    Работает как для звонков (``export/call-features/features``), так и для WhatsApp (``export/whatsapp-features/features``).
    """
    sink, mem = _tee()
    return _run_service(
        lambda: AggregateFeatureService(sink=sink).execute(
            AggregateFeatureRequest(
                features_dir=Path(features_dir),
                output_dir=Path(output_dir),
                limit=limit,
            )
        ),
        mem,
    )


@app.post("/analytics/recommendations")
def generate_recommendations(
    aggregate_path: str = Form(
        "export/call-analytics/aggregate.json",
        description="Путь к aggregate.json из /analytics/aggregate",
    ),
    output_dir: str = Form(
        "export/call-analytics",
        description="Папка для рекомендаций",
    ),
    model: str = Form("gpt-4o", description="OpenAI модель (рекомендуется gpt-4o)"),
    source_label: str = Form("", description="Метка источника (напр. 'звонки апрель 2024')"),
    openai_key: str | None = Security(_openai_key_header),
) -> dict[str, Any]:
    """Генерация рекомендаций по всему отделу продаж на основе агрегированной статистики.

    Отправляет **один** запрос в OpenAI по агрегату (не по каждому звонку отдельно).
    Возвращает: key_findings, patterns, manager_insights, recommendations, score.
    """
    key = _require_openai_key(openai_key)
    sink, mem = _tee()
    return _run_service(
        lambda: GenerateRecommendationsService(
            gateway=OpenAiResponsesClient(key), sink=sink,
        ).execute(
            GenerateRecommendationsRequest(
                aggregate_path=Path(aggregate_path),
                output_dir=Path(output_dir),
                model=model,
                source_label=_none(source_label),
            )
        ),
        mem,
    )


@app.post("/whatsapp-features/extract")
def extract_whatsapp_features(
    conversation_report: str = Form(
        "export/whatsapp-timeline/report.json",
        description="Путь к report.json из /whatsapp/export или /whatsapp-timeline/export",
    ),
    conversation_dir: str = Form(
        "export/whatsapp-timeline/conversations",
        description="Резервная папка с JSON-файлами переписок",
    ),
    output_dir: str = Form("export/whatsapp-features", description="Папка для фич"),
    model: str = Form("gpt-4o-mini", description="OpenAI модель"),
    limit: int = Form(0, description="Максимум переписок (0 = все)"),
    skip_existing: bool = Form(False, description="Пропускать уже обработанные"),
    openai_key: str | None = Security(_openai_key_header),
) -> dict[str, Any]:
    """Извлечение фич из WhatsApp-переписок через OpenAI → ``export/whatsapp-features/``."""
    key = _require_openai_key(openai_key)
    sink, mem = _tee()
    return _run_service(
        lambda: ExtractWhatsAppFeaturesService(
            gateway=OpenAiResponsesClient(key), sink=sink,
        ).execute(
            ExtractWhatsAppFeaturesRequest(
                output_dir=Path(output_dir),
                conversation_report_path=Path(conversation_report),
                conversation_dir=Path(conversation_dir),
                model=model, limit=limit, skip_existing=skip_existing,
            )
        ),
        mem,
    )
