"""FastAPI application exposing the three Bitrix exporters as HTTP endpoints."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from ..application.call_records import CallRecordsScanRequest, CallRecordsScanService
from ..application.crm import CrmExportRequest, CrmExportService
from ..application.whatsapp import WhatsAppExportRequest, WhatsAppExportService
from ..domain.exceptions import DomainError
from ..infrastructure.http import BitrixClient
from ..infrastructure.persistence import FileSystemJsonWriter
from ..infrastructure.persistence.memory_writer import InMemoryJsonSink, TeeJsonSink

# Shown as "Authorize" button in Swagger UI — set once, used by CRM and call-records endpoints.
_webhook_header = APIKeyHeader(
    name="X-Webhook-Url",
    scheme_name="WebhookUrl",
    description="Bitrix24 webhook base URL for CRM and call-records (e.g. https://portal.bitrix24.ru/rest/1/token/)",
    auto_error=False,
)

# Separate webhook for WhatsApp export — set independently in the Authorize dialog.
_whatsapp_webhook_header = APIKeyHeader(
    name="X-Whatsapp-Webhook-Url",
    scheme_name="WhatsappWebhookUrl",
    description="Bitrix24 webhook base URL specifically for WhatsApp export (e.g. https://portal.bitrix24.ru/rest/1/token/)",
    auto_error=False,
)

app = FastAPI(
    title="Bitrix Ingest API",
    version="0.1.0",
    description=(
        "Run Bitrix24 exports and receive results as JSON.\n\n"
        "Set your webhook URL once via the **Authorize** button "
        "(`X-Webhook-Url` header) — it will be sent with every request automatically."
    ),
    swagger_ui_parameters={"persistAuthorization": True},
)

# Same default output directories as the CLI commands.
_CRM_DIR = Path("export")
_CALLS_DIR = Path("export/call-records-scan")
_WA_DIR = Path("export/whatsapp-timeline")


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class CrmExportBody(BaseModel):
    date_from: str | None = None
    date_to: str | None = None
    skip_users: bool = False
    skip_activities: bool = False
    limit: int | None = None


class CallScanBody(BaseModel):
    date_from: str | None = None
    date_to: str | None = None
    limit: int = 20


class WhatsAppExportBody(BaseModel):
    date_from: str | None = None
    date_to: str | None = None
    limit: int = 100
    deal_ids: list[str] | None = None
    exclude_system_messages: bool = False
    page_delay: float = 0.3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_webhook(header_url: str | None) -> str:
    url = header_url
    if not url:
        raise HTTPException(
            status_code=422,
            detail=(
                "webhook_base_url is required. "
                "Provide it in the request body or set X-Webhook-Url via Authorize."
            ),
        )
    return url


def _run(fn: Any, mem: InMemoryJsonSink) -> dict[str, Any]:
    try:
        fn(mem)
    except DomainError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "ok", "data": mem.data}


def _tee() -> tuple[TeeJsonSink, InMemoryJsonSink]:
    mem = InMemoryJsonSink()
    sink = TeeJsonSink(primary=FileSystemJsonWriter(), secondary=mem)
    return sink, mem


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/crm/export")
def export_crm(
    body: CrmExportBody,
    webhook_url: str | None = Security(_webhook_header),
) -> dict[str, Any]:
    """Export CRM snapshot — also saves files to ``export/`` on disk."""
    url = _resolve_webhook(webhook_url)
    sink, mem = _tee()

    def run(s: Any) -> None:
        CrmExportService(gateway=BitrixClient(url), sink=s).execute(
            CrmExportRequest(
                output_dir=_CRM_DIR,
                date_from=body.date_from,
                date_to=body.date_to,
                skip_users=body.skip_users,
                skip_activities=body.skip_activities,
                limit=body.limit,
            )
        )

    return _run(lambda _: run(sink), mem)


@app.post("/call-records/scan")
def scan_call_records(
    body: CallScanBody,
    webhook_url: str | None = Security(_webhook_header),
) -> dict[str, Any]:
    """Scan call records — also saves files to ``export/call-records-scan/`` on disk."""
    url = _resolve_webhook(webhook_url)
    sink, mem = _tee()

    def run(s: Any) -> None:
        CallRecordsScanService(gateway=BitrixClient(url), sink=s).execute(
            CallRecordsScanRequest(
                output_dir=_CALLS_DIR,
                limit=body.limit,
                date_from=body.date_from,
                date_to=body.date_to,
            )
        )

    return _run(lambda _: run(sink), mem)


@app.post("/whatsapp/export")
def export_whatsapp(
    body: WhatsAppExportBody,
    webhook_url: str | None = Security(_whatsapp_webhook_header),
) -> dict[str, Any]:
    """Export WhatsApp conversations — also saves files to ``export/whatsapp-timeline/`` on disk."""
    url = _resolve_webhook(webhook_url)
    sink, mem = _tee()

    def run(s: Any) -> None:
        WhatsAppExportService(
            gateway=BitrixClient(url, page_delay=body.page_delay),
            sink=s,
        ).execute(
            WhatsAppExportRequest(
                output_dir=_WA_DIR,
                limit=body.limit,
                date_from=body.date_from,
                date_to=body.date_to,
                deal_ids=body.deal_ids,
                skip_existing=False,
                include_system_messages=not body.exclude_system_messages,
            )
        )

    return _run(lambda _: run(sink), mem)
