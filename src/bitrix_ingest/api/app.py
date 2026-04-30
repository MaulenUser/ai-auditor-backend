"""FastAPI application exposing the Bitrix exporters and OpenAI pipelines as HTTP endpoints."""
from __future__ import annotations

import json
import os
import logging
import re
import threading
import uuid
import base64
import contextvars
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from fastapi import BackgroundTasks, FastAPI, Form, Header, HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from ..domain.analysis_run import AnalysisRun
from ..domain.business_profile import BusinessProfile
from ..domain.integrations import Integrations
from ..domain.tenant import Tenant
from ..domain.user import User
from ..infrastructure.database import (
    AnalysisRunRepository,
    BusinessProfileRepository,
    IntegrationsRepository,
    TenantRepository,
    UserRepository,
)

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
from ..application.crm import CrmExportRequest, CrmExportService, StageHistoryRequest, StageHistoryService
from ..application.executive_report import BuildExecutiveReportRequest, BuildExecutiveReportService
from ..application.executive_pipeline import RunExecutivePipelineRequest, RunExecutivePipelineService
from ..application.recordings import DownloadRecordingsRequest, DownloadRecordingsService
from ..application.sales_quality import AnalyzeSalesQualityRequest, AnalyzeSalesQualityService
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
from ..infrastructure.audit_trace import AuditTraceRecorder, TracedJsonSink
from ..infrastructure.persistence import FileSystemJsonWriter
from ..infrastructure.persistence.memory_writer import InMemoryJsonSink, TeeJsonSink

logger = logging.getLogger(__name__)

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

_setup_token_header = APIKeyHeader(
    name="X-Setup-Token",
    scheme_name="SetupToken",
    description="Admin setup token for server-side integration provisioning",
    auto_error=False,
)

_authorization_header = APIKeyHeader(
    name="Authorization",
    scheme_name="BearerToken",
    description="Bearer access token from /api/auth/login",
    auto_error=False,
)

# ---------------------------------------------------------------------------

app = FastAPI(
    title="Bitrix Ingest API",
    version="0.2.0",
    description=(
        "Bitrix24 экспорт и OpenAI-пайплайн в одном интерфейсе.\n\n"
        "Нажмите **Authorize** и укажите:\n"
        "- `X-Webhook-Url` — для CRM / звонков\n"
        "- `X-Whatsapp-Webhook-Url` — для WhatsApp\n"
        "- `X-OpenAI-Api-Key` — для транскрипции и извлечения фич\n\n"
        "Передайте заголовок `X-Tenant-Id` (например `sapaplast`) для изоляции данных клиента.\n"
        "Если заголовок не передан, используется tenant `default`."
    ),
    swagger_ui_parameters={"persistAuthorization": True},
)

_DB_PATH = Path(os.environ.get("BITRIX_DB_PATH", "data/app.db"))

_EXECUTIVE_REPORT_JOBS: dict[str, dict[str, Any]] = {}
_EXECUTIVE_REPORT_JOBS_LOCK = threading.Lock()
_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,128}$")
_ROLE_VALUES = {"admin", "client"}
_AUTH_CONTEXT: contextvars.ContextVar[User | None] = contextvars.ContextVar(
    "auth_user",
    default=None,
)


# ---------------------------------------------------------------------------
# Repository factories
# ---------------------------------------------------------------------------


def _tenant_repo() -> TenantRepository:
    return TenantRepository(_DB_PATH)


def _profile_repo(tenant_id: str = "default") -> BusinessProfileRepository:
    return BusinessProfileRepository(_DB_PATH, tenant_id)


def _integrations_repo(tenant_id: str = "default") -> IntegrationsRepository:
    return IntegrationsRepository(_DB_PATH, tenant_id)


def _runs_repo() -> AnalysisRunRepository:
    return AnalysisRunRepository(_DB_PATH)


def _user_repo() -> UserRepository:
    return UserRepository(_DB_PATH)


def _get_integrations(tenant_id: str = "default") -> Integrations:
    return _integrations_repo(tenant_id).get() or Integrations()


def _flag_enabled(*names: str) -> bool:
    for name in names:
        value = os.environ.get(name, "").strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
    return False


def _auth_required() -> bool:
    return _flag_enabled("AI_AUDITOR_AUTH_REQUIRED", "AUTH_REQUIRED")


def _auth_secret() -> str:
    secret = (
        os.environ.get("AI_AUDITOR_AUTH_SECRET", "").strip()
        or os.environ.get("AUTH_SECRET", "").strip()
    )
    if not secret:
        if _auth_required():
            raise HTTPException(
                status_code=503,
                detail="AI_AUDITOR_AUTH_SECRET is not configured on the backend.",
            )
        return "dev-only-ai-auditor-auth-secret"
    return secret


def _token_ttl_seconds() -> int:
    raw = os.environ.get("AI_AUDITOR_AUTH_TOKEN_TTL_SECONDS", "").strip()
    try:
        ttl = int(raw or "86400")
    except ValueError:
        ttl = 86400
    return max(300, ttl)


def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64_decode(data: str) -> bytes:
    padding = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _hash_password(password: str, salt: str | None = None) -> str:
    if not password:
        raise HTTPException(status_code=422, detail="Password cannot be empty.")
    rounds = 260_000
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), rounds)
    return f"pbkdf2_sha256${rounds}${salt}${_b64_encode(digest)}"


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, rounds_raw, salt, digest = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        rounds = int(rounds_raw)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            rounds,
        )
        return hmac.compare_digest(_b64_encode(actual), digest)
    except Exception:
        return False


def _create_access_token(user: User) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {
        "sub": user.username,
        "tenant_id": user.tenant_id,
        "role": user.role,
        "iat": now,
        "exp": now + _token_ttl_seconds(),
    }
    signing_input = ".".join(
        (
            _b64_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        )
    )
    signature = hmac.new(
        _auth_secret().encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{signing_input}.{_b64_encode(signature)}"


def _decode_access_token(token: str) -> User:
    credentials_error = HTTPException(status_code=401, detail="Invalid or expired auth token.")
    try:
        header_b64, payload_b64, signature_b64 = token.split(".", 2)
        signing_input = f"{header_b64}.{payload_b64}"
        expected_signature = hmac.new(
            _auth_secret().encode("utf-8"),
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64_encode(expected_signature), signature_b64):
            raise credentials_error
        payload = json.loads(_b64_decode(payload_b64).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            raise credentials_error
        username = str(payload.get("sub") or "").strip()
    except HTTPException:
        raise
    except Exception as exc:
        raise credentials_error from exc

    user = _user_repo().get(username)
    if not user or not user.active:
        raise credentials_error
    return user


def _bearer_token(authorization: str | None) -> str | None:
    value = (authorization or "").strip()
    if not value:
        return None
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return None


def _current_user() -> User | None:
    return _AUTH_CONTEXT.get()


def _require_user() -> User:
    user = _current_user()
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def _require_admin_or_dev() -> User | None:
    user = _current_user()
    if not user:
        if _auth_required():
            raise HTTPException(status_code=401, detail="Authentication required.")
        return None
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin role required.")
    return user


def _public_auth_path(path: str) -> bool:
    return (
        path in {"/health", "/openapi.json", "/api/auth/login", "/api/auth/status", "/api/auth/bootstrap"}
        or path.startswith("/docs")
        or path.startswith("/redoc")
    )


def _validate_tenant_id(tid: str) -> None:
    if not _TENANT_ID_RE.fullmatch(tid):
        raise HTTPException(
            status_code=422,
            detail="Invalid X-Tenant-Id. Use 1-64 characters: letters, digits, underscore, hyphen.",
        )


def _resolve_tenant_id(x_tenant_id: str | None) -> str:
    """Resolve tenant from auth token; X-Tenant-Id is allowed only for admins/dev."""
    user = _current_user()
    requested = (x_tenant_id or "").strip()
    if user:
        if user.is_admin:
            tid = requested or user.tenant_id or "default"
        else:
            if requested and requested != user.tenant_id:
                raise HTTPException(status_code=403, detail="Tenant access denied.")
            tid = user.tenant_id
    else:
        if _auth_required():
            raise HTTPException(status_code=401, detail="Authentication required.")
        tid = requested or "default"
    _validate_tenant_id(tid)
    _tenant_repo().ensure(tid)
    return tid


def _ensure_tenant_access(tenant_id: str) -> str:
    _validate_tenant_id(tenant_id)
    user = _current_user()
    if user and not user.is_admin and tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant access denied.")
    if not user and _auth_required():
        raise HTTPException(status_code=401, detail="Authentication required.")
    return tenant_id


def _tenant_storage(tenant_id: str, run_id: str) -> Path:
    return Path(f"storage/tenants/{tenant_id}/runs/{run_id}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _none(value: str | None) -> str | None:
    """Treat form strings 'null', 'none', '' as None."""
    if not value or value.strip().lower() in ("null", "none"):
        return None
    return value


def _resolve_webhook(header_url: str | None, tenant_id: str = "default") -> str:
    """Header takes priority; falls back to DB bitrix_webhook_url for the tenant."""
    url = header_url or _get_integrations(tenant_id).bitrix_webhook_url
    if not url:
        raise HTTPException(
            status_code=422,
            detail="Webhook URL не настроен. Укажите X-Webhook-Url в Authorize или сохраните его на странице настроек.",
        )
    return url


def _resolve_whatsapp_webhook(
    header_url: str | None,
    crm_fallback: str | None = None,
    tenant_id: str = "default",
) -> str:
    """Header → DB whatsapp_webhook_url → DB bitrix_webhook_url → crm_fallback."""
    if header_url:
        return header_url
    ints = _get_integrations(tenant_id)
    url = ints.whatsapp_webhook_url or ints.bitrix_webhook_url or crm_fallback
    if not url:
        raise HTTPException(
            status_code=422,
            detail="WhatsApp Webhook URL не настроен. Укажите X-Whatsapp-Webhook-Url в Authorize или сохраните его на странице настроек.",
        )
    return url


def _resolve_openai_key(header_key: str | None, tenant_id: str = "default") -> str:
    """Header takes priority; falls back to DB openai_api_key for the tenant."""
    key = header_key or _get_integrations(tenant_id).openai_api_key
    if not key:
        raise HTTPException(
            status_code=422,
            detail="OpenAI API Key не настроен. Укажите X-OpenAI-Api-Key в Authorize или сохраните его на странице настроек.",
        )
    return key


def _require_setup_token(header_token: str | None) -> None:
    expected = os.environ.get("SETUP_INTEGRATIONS_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="SETUP_INTEGRATIONS_TOKEN is not configured on the backend.",
        )
    if header_token != expected:
        raise HTTPException(status_code=403, detail="Invalid setup token.")


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


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _clean_form_list(values: list[str] | None) -> list[str] | None:
    clean = [item for item in (values or []) if _none(item)]
    return clean or None


def _set_executive_report_job(job_id: str, **updates: Any) -> None:
    with _EXECUTIVE_REPORT_JOBS_LOCK:
        job = _EXECUTIVE_REPORT_JOBS.setdefault(job_id, {"job_id": job_id})
        job.update(updates)
        job["updated_at"] = _now_iso()
    if "status" in updates:
        try:
            _runs_repo().update_status(
                run_id=job_id,
                status=updates["status"],
                completed_at=updates.get("completed_at"),
                error=updates.get("error"),
            )
        except Exception:
            logger.warning("Failed to persist run status to SQLite: %s", job_id)


def _get_executive_report_job(job_id: str) -> dict[str, Any] | None:
    with _EXECUTIVE_REPORT_JOBS_LOCK:
        job = _EXECUTIVE_REPORT_JOBS.get(job_id)
        return dict(job) if job else None


def _load_json_if_exists(path: Path) -> Any | None:
    if not path.exists() or not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _redact_error_message(message: object, *secrets: str | None) -> str:
    text = str(message)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _execute_executive_pipeline(
    *,
    request: RunExecutivePipelineRequest,
    crm_url: str,
    whatsapp_url: str,
    openai_key: str,
    sink: Any,
) -> None:
    RunExecutivePipelineService(
        crm_gateway=BitrixClient(crm_url, call_delay=0.2, page_delay=0.2),
        whatsapp_gateway=BitrixClient(whatsapp_url, call_delay=0.2, page_delay=0.2),
        responses_gateway=OpenAiResponsesClient(openai_key),
        transcription_gateway=OpenAiTranscriptionClient(openai_key),
        file_downloader=RequestsFileDownloader(),
        sink=sink,
    ).execute(request)


def _run_executive_report_background_job(
    *,
    job_id: str,
    request: RunExecutivePipelineRequest,
    crm_url: str,
    whatsapp_url: str,
    openai_key: str,
) -> None:
    _set_executive_report_job(job_id, status="running", started_at=_now_iso())
    try:
        _execute_executive_pipeline(
            request=request,
            crm_url=crm_url,
            whatsapp_url=whatsapp_url,
            openai_key=openai_key,
            sink=FileSystemJsonWriter(),
        )
        report_path = request.executive_report_dir / "executive-report.json"
        summary_path = request.executive_report_dir / "pipeline-summary.json"
        _set_executive_report_job(
            job_id,
            status="completed",
            completed_at=_now_iso(),
            report_path=str(report_path),
            summary_path=str(summary_path),
            executive_report=_load_json_if_exists(report_path),
            pipeline_summary=_load_json_if_exists(summary_path),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Executive report job failed: %s", job_id)
        _set_executive_report_job(
            job_id,
            status="error",
            completed_at=_now_iso(),
            error=_redact_error_message(exc, crm_url, whatsapp_url, openai_key),
            error_type=type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Authentication middleware
# ---------------------------------------------------------------------------


@app.middleware("http")
async def _auth_middleware(request: Request, call_next: Any) -> JSONResponse:
    token_value = _bearer_token(request.headers.get("authorization"))
    user: User | None = None
    if token_value:
        try:
            user = _decode_access_token(token_value)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    elif _auth_required() and request.method != "OPTIONS" and not _public_auth_path(request.url.path):
        return JSONResponse(status_code=401, content={"detail": "Authentication required."})

    context_token = _AUTH_CONTEXT.set(user)
    try:
        return await call_next(request)
    finally:
        _AUTH_CONTEXT.reset(context_token)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


class _LoginPayload(BaseModel):
    username: str
    password: str


class _UserPayload(BaseModel):
    username: str
    password: str
    tenant_id: str = "default"
    role: str = "client"
    active: bool = True


def _normalize_username(username: str) -> str:
    value = username.strip().lower()
    if not _USERNAME_RE.fullmatch(value):
        raise HTTPException(
            status_code=422,
            detail="Username must be 3-128 characters: letters, digits, dot, underscore, @, hyphen.",
        )
    return value


def _normalize_role(role: str) -> str:
    value = (role or "client").strip().lower()
    if value not in _ROLE_VALUES:
        raise HTTPException(status_code=422, detail="Role must be 'admin' or 'client'.")
    return value


def _user_from_payload(payload: _UserPayload) -> User:
    username = _normalize_username(payload.username)
    tenant_id = payload.tenant_id.strip() or "default"
    _validate_tenant_id(tenant_id)
    _tenant_repo().ensure(tenant_id)
    return User(
        username=username,
        tenant_id=tenant_id,
        role=_normalize_role(payload.role),
        password_hash=_hash_password(payload.password),
        active=bool(payload.active),
    )


@app.get("/api/auth/status", tags=["Auth"])
def get_auth_status() -> dict[str, Any]:
    return {
        "auth_required": _auth_required(),
        "has_users": _user_repo().count() > 0,
    }


@app.post("/api/auth/login", tags=["Auth"])
def login(payload: _LoginPayload) -> dict[str, Any]:
    username = _normalize_username(payload.username)
    user = _user_repo().get(username)
    if not user or not user.active or not _verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    return {
        "access_token": _create_access_token(user),
        "token_type": "bearer",
        "expires_in": _token_ttl_seconds(),
        "user": user.to_public_dict(),
    }


@app.get("/api/auth/me", tags=["Auth"])
def get_current_user(
    authorization: str | None = Security(_authorization_header),
) -> dict[str, Any]:
    del authorization
    user = _require_user()
    return {"user": user.to_public_dict()}


@app.post("/api/auth/bootstrap", tags=["Auth"])
def bootstrap_first_admin(
    payload: _UserPayload,
    setup_token: str | None = Security(_setup_token_header),
) -> dict[str, Any]:
    """Create the first admin user using X-Setup-Token."""
    _require_setup_token(setup_token)
    if _user_repo().count() > 0:
        raise HTTPException(status_code=409, detail="Users already exist.")
    user = _user_from_payload(_UserPayload(
        username=payload.username,
        password=payload.password,
        tenant_id=payload.tenant_id,
        role="admin",
        active=True,
    ))
    _user_repo().save(user)
    return {"status": "ok", "user": user.to_public_dict()}


@app.get("/api/users", tags=["Auth"])
def list_users(
    authorization: str | None = Security(_authorization_header),
) -> dict[str, Any]:
    del authorization
    _require_admin_or_dev()
    return {"users": [user.to_public_dict() for user in _user_repo().list_all()]}


@app.post("/api/users", tags=["Auth"])
def create_user(
    payload: _UserPayload,
    authorization: str | None = Security(_authorization_header),
) -> dict[str, Any]:
    del authorization
    _require_admin_or_dev()
    user = _user_from_payload(payload)
    _user_repo().save(user)
    return {"status": "ok", "user": user.to_public_dict()}


# ---------------------------------------------------------------------------
# Executive Report endpoints
# ---------------------------------------------------------------------------


@app.get("/executive-report/latest", tags=["Executive Report"])
def get_latest_executive_report(
    report_path: str = Query(
        "",
        description=(
            "Path to executive-report.json. "
            "If empty, the latest completed run for the tenant is used."
        ),
    ),
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    tid = _resolve_tenant_id(x_tenant_id)
    resolved_path: str = report_path
    if not resolved_path:
        runs = _runs_repo().list_by_tenant(tid)
        completed = [r for r in runs if r.status == "completed"]
        if not completed:
            raise HTTPException(
                status_code=404,
                detail=f"No completed runs found for tenant '{tid}'.",
            )
        resolved_path = str(Path(completed[0].output_dir) / "executive-report.json")

    path = Path(resolved_path)
    if report_path:
        tenant_root = (Path("storage") / "tenants" / tid).resolve()
        resolved_report_path = path.resolve()
        if not resolved_report_path.is_relative_to(tenant_root):
            raise HTTPException(
                status_code=403,
                detail="report_path must point inside the current tenant storage.",
            )
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Report not found: {path}")
    if not path.is_file():
        raise HTTPException(status_code=422, detail=f"Report path is not a file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON report: {path}") from exc


@app.post("/executive-report/build", tags=["Executive Report"])
def build_executive_report(
    sales_quality_dir: str = Form("export/sales-quality", description="Directory with sales-quality report.json and features"),
    output_dir: str = Form("export/executive-report", description="Output directory"),
    scope: str = Form("analyzed", description="analyzed or bitrix"),
    date_from: Optional[str] = Form(None, description="Start date for Bitrix scope"),
    date_to: Optional[str] = Form(None, description="End date for Bitrix scope"),
    category_id: Optional[List[str]] = Form(None, description="Deal category IDs"),
    responsible_id: Optional[List[str]] = Form(None, description="ASSIGNED_BY_ID; may be repeated"),
    deal_id: Optional[List[str]] = Form(None, description="Specific deal IDs"),
    limit: int = Form(0, description="Max deals, 0 = all"),
    average_ticket_kzt: Optional[float] = Form(None, description="Average ticket for lost revenue formula"),
    expected_conversion_pct: Optional[float] = Form(None, description="Expected conversion percent for lost revenue formula"),
    portal_base_url: str = Form("https://sapaplast.bitrix24.kz", description="Bitrix portal URL for CRM links"),
    max_reanimation_cards: int = Form(100, description="Max failed deal cards"),
    webhook_url: str | None = Security(_webhook_header),
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Build the executive report from sales-quality outputs and Bitrix CRM."""
    tid = _resolve_tenant_id(x_tenant_id)
    url = _resolve_webhook(webhook_url, tid)
    clean_categories = [item for item in (category_id or []) if _none(item)] or None
    clean_responsible = [item for item in (responsible_id or []) if _none(item)] or None
    clean_deals = [item for item in (deal_id or []) if _none(item)] or None
    sink, mem = _tee()
    return _run_service(
        lambda: BuildExecutiveReportService(
            gateway=BitrixClient(url, call_delay=0.2),
            sink=sink,
        ).execute(
            BuildExecutiveReportRequest(
                output_dir=Path(output_dir),
                sales_quality_dir=Path(sales_quality_dir),
                scope=scope,
                date_from=_none(date_from),
                date_to=_none(date_to),
                category_ids=clean_categories,
                responsible_id=clean_responsible[0] if clean_responsible else None,
                responsible_ids=clean_responsible,
                deal_ids=clean_deals,
                limit=limit,
                average_ticket_kzt=average_ticket_kzt,
                expected_conversion_pct=expected_conversion_pct,
                portal_base_url=portal_base_url,
                max_reanimation_cards=max_reanimation_cards,
            )
        ),
        mem,
    )


@app.post("/executive-report/run", tags=["Executive Report"])
def run_executive_report_pipeline(
    background_tasks: BackgroundTasks,
    sales_quality_dir: str = Form("", description="Directory for sales-quality outputs (empty = per-tenant/run path)"),
    output_dir: str = Form("", description="Executive report output directory (empty = per-tenant/run path)"),
    whatsapp_dir: str = Form("", description="WhatsApp export directory (empty = per-tenant/run path)"),
    call_scan_dir: str = Form("", description="Call scan directory (empty = per-tenant/run path)"),
    recordings_dir: str = Form("", description="Call recordings directory (empty = per-tenant/run path)"),
    date_from: Optional[str] = Form(None, description="Start date"),
    date_to: Optional[str] = Form(None, description="End date"),
    category_id: Optional[List[str]] = Form(None, description="Deal category IDs"),
    responsible_id: Optional[List[str]] = Form(None, description="ASSIGNED_BY_ID; may be repeated"),
    deal_id: Optional[List[str]] = Form(None, description="Specific deal IDs"),
    limit: int = Form(0, description="Max CRM deals, 0 = all"),
    model: str = Form("gpt-4o-mini", description="OpenAI model for sales-quality analysis"),
    transcription_model: str = Form("gpt-4o-transcribe", description="OpenAI transcription model"),
    average_ticket_kzt: Optional[float] = Form(None, description="Average ticket for lost revenue formula"),
    expected_conversion_pct: Optional[float] = Form(None, description="Expected conversion percent"),
    portal_base_url: str = Form("https://sapaplast.bitrix24.kz", description="Bitrix portal URL for CRM links"),
    max_reanimation_cards: int = Form(100, description="Max failed deal cards"),
    reset_outputs: bool = Form(True, description="Clear output directories before running"),
    include_whatsapp_audio: bool = Form(False, description="Download and transcribe WhatsApp audio messages"),
    wait: bool = Form(False, description="Run synchronously and wait for completion"),
    crm_webhook_url: str | None = Security(_webhook_header),
    whatsapp_webhook_url: str | None = Security(_whatsapp_webhook_header),
    openai_key: str | None = Security(_openai_key_header),
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Run source refresh, sales-quality analysis, and executive report in one scope."""
    tid = _resolve_tenant_id(x_tenant_id)
    crm_url = _resolve_webhook(crm_webhook_url, tid)
    whatsapp_url = _resolve_whatsapp_webhook(whatsapp_webhook_url, crm_url, tid)
    key = _resolve_openai_key(openai_key, tid)
    clean_categories = _clean_form_list(category_id)
    clean_responsible = _clean_form_list(responsible_id)
    clean_deals = _clean_form_list(deal_id)

    job_id = uuid.uuid4().hex
    base = _tenant_storage(tid, job_id)

    resolved_output = Path(output_dir) if output_dir else base / "executive-report"
    resolved_sq = Path(sales_quality_dir) if sales_quality_dir else base / "sales-quality"
    resolved_wa = Path(whatsapp_dir) if whatsapp_dir else base / "whatsapp-timeline"
    resolved_calls = Path(call_scan_dir) if call_scan_dir else base / "call-records-scan"
    resolved_recordings = Path(recordings_dir) if recordings_dir else base / "recordings"

    request = RunExecutivePipelineRequest(
        sales_quality_dir=resolved_sq,
        executive_report_dir=resolved_output,
        whatsapp_dir=resolved_wa,
        call_scan_dir=resolved_calls,
        recordings_dir=resolved_recordings,
        date_from=_none(date_from),
        date_to=_none(date_to),
        category_ids=clean_categories,
        responsible_ids=clean_responsible,
        deal_ids=clean_deals,
        limit=limit,
        model=model,
        transcription_model=transcription_model,
        average_ticket_kzt=average_ticket_kzt,
        expected_conversion_pct=expected_conversion_pct,
        portal_base_url=portal_base_url,
        max_reanimation_cards=max_reanimation_cards,
        include_whatsapp_audio=include_whatsapp_audio,
        reset_outputs=reset_outputs,
    )

    if wait:
        sink, mem = _tee()
        result = _run_service(
            lambda: _execute_executive_pipeline(
                request=request,
                crm_url=crm_url,
                whatsapp_url=whatsapp_url,
                openai_key=key,
                sink=sink,
            ),
            mem,
        )
        report_path = resolved_output / "executive-report.json"
        if report_path.exists():
            result["executive_report"] = json.loads(report_path.read_text(encoding="utf-8"))
        return result

    _set_executive_report_job(
        job_id,
        tenant_id=tid,
        status="queued",
        queued_at=_now_iso(),
        output_dir=str(resolved_output),
        scope={
            "date_from": request.date_from,
            "date_to": request.date_to,
            "category_ids": request.category_ids or [],
            "responsible_ids": request.responsible_ids or [],
            "deal_ids": request.deal_ids or [],
            "limit": request.limit,
            "include_whatsapp_audio": request.include_whatsapp_audio,
        },
    )
    try:
        _runs_repo().create(AnalysisRun(
            run_id=job_id,
            tenant_id=tid,
            status="queued",
            date_from=request.date_from,
            date_to=request.date_to,
            category_ids=request.category_ids or [],
            responsible_ids=request.responsible_ids or [],
            output_dir=str(resolved_output),
        ))
    except Exception:
        logger.warning("Failed to persist new run to SQLite: %s", job_id)

    background_tasks.add_task(
        _run_executive_report_background_job,
        job_id=job_id,
        request=request,
        crm_url=crm_url,
        whatsapp_url=whatsapp_url,
        openai_key=key,
    )
    return {
        "status": "started",
        "job_id": job_id,
        "tenant_id": tid,
        "output_dir": str(resolved_output),
        "status_url": f"/executive-report/jobs/{job_id}",
    }


@app.get("/executive-report/jobs/{job_id}", tags=["Executive Report"])
def get_executive_report_job(
    job_id: str,
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Return status for a background executive report run."""
    tid = _resolve_tenant_id(x_tenant_id)
    job = _get_executive_report_job(job_id)
    if not job:
        # Fall back to SQLite for runs from previous server sessions.
        run = _runs_repo().get(job_id)
        if not run:
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        if run.tenant_id != tid:
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        return run.to_dict()
    if job.get("tenant_id") and job.get("tenant_id") != tid:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return job


# ---------------------------------------------------------------------------
# App state, tenants, and settings — persisted in SQLite
# ---------------------------------------------------------------------------


@app.get("/api/app-state", tags=["Settings"])
def get_app_state(x_tenant_id: str | None = Header(None)) -> dict[str, Any]:
    """Returns current app state including business profile and integrations status."""
    tid = _resolve_tenant_id(x_tenant_id)
    profile = _profile_repo(tid).get()
    integrations = _get_integrations(tid)
    return {
        "tenant_id": tid,
        "setup": {
            "business_profile": profile.to_dict() if profile else {},
            "integrations": integrations.to_status_dict(),
        },
    }


class _BusinessProfilePayload(BaseModel):
    company_name: str = ""
    website_url: str = ""
    instagram_url: str = ""
    price_list: str = ""
    average_ticket_kzt: float | None = None
    advantages: str = ""
    promotions: str = ""


@app.post("/api/setup-profile", tags=["Settings"])
def setup_profile(
    payload: _BusinessProfilePayload,
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Save business profile to SQLite and return updated app state."""
    tid = _resolve_tenant_id(x_tenant_id)
    profile = BusinessProfile.from_dict(payload.model_dump())
    _profile_repo(tid).save(profile)
    integrations = _get_integrations(tid)
    return {
        "app_state": {
            "tenant_id": tid,
            "setup": {
                "business_profile": profile.to_dict(),
                "integrations": integrations.to_status_dict(),
            },
        }
    }


class _IntegrationsPayload(BaseModel):
    bitrix_webhook_url: str = ""
    whatsapp_webhook_url: str = ""
    openai_api_key: str = ""


@app.get("/api/integrations", tags=["Settings"])
def get_integrations(x_tenant_id: str | None = Header(None)) -> dict[str, Any]:
    """Returns current integration configuration status (no raw secrets)."""
    tid = _resolve_tenant_id(x_tenant_id)
    ints = _get_integrations(tid)
    return {"tenant_id": tid, "integrations": ints.to_status_dict()}


@app.post("/api/setup-integrations", tags=["Settings"])
def setup_integrations(
    payload: _IntegrationsPayload,
    x_tenant_id: str | None = Header(None),
    setup_token: str | None = Security(_setup_token_header),
) -> dict[str, Any]:
    """Save Bitrix webhook URLs and OpenAI API key to SQLite."""
    _require_setup_token(setup_token)
    tid = _resolve_tenant_id(x_tenant_id)
    current = _get_integrations(tid)
    incoming = Integrations.from_dict(payload.model_dump())
    ints = Integrations(
        bitrix_webhook_url=incoming.bitrix_webhook_url or current.bitrix_webhook_url,
        whatsapp_webhook_url=incoming.whatsapp_webhook_url or current.whatsapp_webhook_url,
        openai_api_key=incoming.openai_api_key or current.openai_api_key,
    )
    _integrations_repo(tid).save(ints)
    return {
        "status": "ok",
        "tenant_id": tid,
        "integrations": ints.to_status_dict(),
    }


# ---------------------------------------------------------------------------
# Tenant CRUD
# ---------------------------------------------------------------------------


class _TenantPayload(BaseModel):
    id: str
    name: str = ""


@app.get("/api/tenants", tags=["Settings"])
def list_tenants(
    authorization: str | None = Security(_authorization_header),
) -> dict[str, Any]:
    """List all tenants."""
    del authorization
    _require_admin_or_dev()
    tenants = _tenant_repo().list_all()
    return {"tenants": [t.to_dict() for t in tenants]}


@app.post("/api/tenants", tags=["Settings"])
def create_tenant(
    payload: _TenantPayload,
    authorization: str | None = Security(_authorization_header),
) -> dict[str, Any]:
    """Create or update a tenant."""
    del authorization
    _require_admin_or_dev()
    tid = payload.id.strip()
    if not tid:
        raise HTTPException(status_code=422, detail="Tenant ID cannot be empty.")
    _validate_tenant_id(tid)
    tenant = Tenant(id=tid, name=payload.name or tid)
    _tenant_repo().save(tenant)
    return {"status": "ok", "tenant": tenant.to_dict()}


@app.get("/api/tenants/{tenant_id}", tags=["Settings"])
def get_tenant(
    tenant_id: str,
    authorization: str | None = Security(_authorization_header),
) -> dict[str, Any]:
    """Get a single tenant by ID."""
    del authorization
    _ensure_tenant_access(tenant_id)
    tenant = _tenant_repo().get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail=f"Tenant not found: {tenant_id}")
    return {"tenant": tenant.to_dict()}


# ---------------------------------------------------------------------------
# Analysis runs
# ---------------------------------------------------------------------------


@app.get("/api/analysis-runs", tags=["Settings"])
def list_analysis_runs(x_tenant_id: str | None = Header(None)) -> dict[str, Any]:
    """List all analysis runs for the current tenant (newest first)."""
    tid = _resolve_tenant_id(x_tenant_id)
    runs = _runs_repo().list_by_tenant(tid)
    return {"tenant_id": tid, "runs": [r.to_dict() for r in runs]}


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
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Возвращает список воронок (crm.dealcategory.list) для выбора в UI."""
    tid = _resolve_tenant_id(x_tenant_id)
    url = _resolve_webhook(webhook_url, tid)
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
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Возвращает список пользователей портала (user.get) для выбора менеджера."""
    tid = _resolve_tenant_id(x_tenant_id)
    url = _resolve_webhook(webhook_url, tid)
    try:
        managers = GetCatalogService(gateway=BitrixClient(url)).get_managers()
    except DomainError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"managers": managers}


@app.get(
    "/catalog/funnels-with-managers",
    summary="Воронки с менеджерами",
    tags=["Catalog"],
)
def get_funnels_with_managers(
    date_from: Optional[str] = Query(
        None,
        description="Дата начала (ISO 8601); "
        "ограничивает "
        "сделки для поиска "
        "менеджеров",
    ),
    date_to: Optional[str] = Query(
        None,
        description="Дата конца (ISO 8601); "
        "ограничивает "
        "сделки для поиска "
        "менеджеров",
    ),
    active_only: bool = Query(
        False,
        description="Возвращать "
        "только активных "
        "менеджеров",
    ),
    webhook_url: str | None = Security(_webhook_header),
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Возвращает воронки и менеджеров по сделкам в каждой воронке."""
    tid = _resolve_tenant_id(x_tenant_id)
    url = _resolve_webhook(webhook_url, tid)
    try:
        funnels = GetCatalogService(gateway=BitrixClient(url)).get_funnels_with_managers(
            date_from=_none(date_from),
            date_to=_none(date_to),
            active_only=active_only,
        )
    except DomainError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"funnels": funnels}


@app.get(
    "/audit/preview",
    summary="Предварительный расчёт аудита",
    tags=["Audit"],
)
def preview_audit(
    funnel_id: Optional[List[str]] = Query(None, description="ID воронок (можно несколько: ?funnel_id=2&funnel_id=4)"),
    date_from: Optional[str] = Query(None, description="Дата начала (ISO 8601)"),
    date_to: Optional[str] = Query(None, description="Дата конца (ISO 8601)"),
    responsible_id: Optional[str] = Query(None, description="ID ответственного по сделке (ASSIGNED_BY_ID; пусто = весь отдел)"),
    webhook_url: str | None = Security(_whatsapp_webhook_header),
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Считает сколько обращений, сотрудников и сделок попадёт в аудит **без** запуска экспорта."""
    tid = _resolve_tenant_id(x_tenant_id)
    url = _resolve_whatsapp_webhook(webhook_url, tenant_id=tid)
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
    responsible_id: Optional[str] = Form(None, description="ID ответственного по сделке (ASSIGNED_BY_ID; пусто = весь отдел)"),
    limit: int = Form(0, description="Лимит сделок (0 = все)"),
    model: str = Form("gpt-4o-mini", description="OpenAI модель для извлечения фич"),
    recommendations_model: str = Form("gpt-4o", description="OpenAI модель для рекомендаций"),
    source_label: Optional[str] = Form(None, description="Метка источника в отчёте"),
    output_dir: str = Form("export/audit", description="Базовая папка вывода"),
    crm_webhook_url: str | None = Security(_webhook_header),
    whatsapp_webhook_url: str | None = Security(_whatsapp_webhook_header),
    openai_key: str | None = Security(_openai_key_header),
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Полный AI-аудит: WhatsApp-переписки → фичи → агрегат → рекомендации."""
    tid = _resolve_tenant_id(x_tenant_id)
    bitrix_url = _resolve_whatsapp_webhook(whatsapp_webhook_url, tenant_id=tid)
    key = _resolve_openai_key(openai_key, tid)

    clean_funnels = [f for f in (funnel_id or []) if _none(f)] or None
    resolved_responsible = _none(responsible_id)

    funnel_slug = ("funnels_" + "_".join(clean_funnels)) if clean_funnels else "all_funnels"
    resolved_output = Path(output_dir) / funnel_slug
    trace_path = resolved_output / "audit-run.trace.json"

    _CALL_DELAY = 0.2

    sink, mem = _tee()
    trace = AuditTraceRecorder(
        trace_path,
        run_name="audit.run",
        request_details={
            "funnel_ids": clean_funnels or [],
            "date_from": _none(date_from),
            "date_to": _none(date_to),
            "responsible_id": resolved_responsible,
            "limit": limit,
            "model": model,
            "recommendations_model": recommendations_model,
            "source_label": _none(source_label) or "",
            "output_dir": resolved_output,
            "call_audit_enabled": bool(crm_webhook_url),
        },
    )
    traced_sink = TracedJsonSink(sink, trace)
    request_payload = RunAuditRequest(
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
    try:
        RunAuditService(
            bitrix_gateway=BitrixClient(
                bitrix_url,
                call_delay=_CALL_DELAY,
                trace=trace,
                trace_name="bitrix.whatsapp",
            ),
            responses_gateway=OpenAiResponsesClient(
                key,
                trace=trace,
                trace_name="openai.responses",
            ),
            sink=traced_sink,
            call_gateway=BitrixClient(
                crm_webhook_url,
                call_delay=_CALL_DELAY,
                trace=trace,
                trace_name="bitrix.crm",
            ) if crm_webhook_url else None,
            transcription_gateway=OpenAiTranscriptionClient(
                key,
                trace=trace,
                trace_name="openai.transcription",
            ) if crm_webhook_url else None,
            file_downloader=RequestsFileDownloader(
                trace=trace,
                trace_name="recording.download",
            ) if crm_webhook_url else None,
            trace=trace,
        ).execute(request_payload)
    except (FileNotFoundError, ValueError) as exc:
        trace.finish(status="error", error=exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DomainError as exc:
        trace.finish(status="error", error=exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        trace.finish(status="error", error=exc)
        raise
    trace.finish(status="ok")
    return {
        "status": "ok",
        "data": mem.data,
        "meta": {
            "trace_file": trace_path.as_posix(),
        },
    }


@app.post("/crm/export")
def export_crm(
    date_from: Optional[str] = Form(None, description="Дата начала (ISO 8601)"),
    date_to: Optional[str] = Form(None, description="Дата конца (ISO 8601)"),
    skip_users: bool = Form(False, description="Пропустить экспорт пользователей"),
    skip_activities: bool = Form(False, description="Пропустить экспорт активностей"),
    limit: Optional[int] = Form(None, description="Максимальное число сделок (пусто = все)"),
    webhook_url: str | None = Security(_webhook_header),
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Экспорт CRM-снапшота → ``export/``."""
    tid = _resolve_tenant_id(x_tenant_id)
    url = _resolve_webhook(webhook_url, tid)
    sink, mem = _tee()
    return _run_service(
        lambda: CrmExportService(gateway=BitrixClient(url), sink=sink).execute(
            CrmExportRequest(
                output_dir=Path("export"),
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
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Сканирование записей звонков → ``export/call-records-scan/``."""
    tid = _resolve_tenant_id(x_tenant_id)
    url = _resolve_webhook(webhook_url, tid)
    sink, mem = _tee()
    return _run_service(
        lambda: CallRecordsScanService(gateway=BitrixClient(url), sink=sink).execute(
            CallRecordsScanRequest(
                output_dir=Path("export/call-records-scan"), limit=limit,
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


@app.post(
    "/crm/stage-history",
    summary="История переходов по стадиям",
    tags=["CRM"],
)
def export_stage_history(
    date_from: Optional[str] = Form(None, description="Дата начала (ISO 8601)"),
    date_to: Optional[str] = Form(None, description="Дата конца (ISO 8601)"),
    funnel_id: Optional[List[str]] = Form(None, description="ID воронок"),
    deal_ids: Optional[List[str]] = Form(None, description="ID конкретных сделок"),
    responsible_id: Optional[str] = Form(None, description="ID ответственного менеджера"),
    whatsapp_only: bool = Form(False, description="Только WhatsApp-сделки"),
    limit: int = Form(0, description="Максимум сделок (0 = все)"),
    skip_existing: bool = Form(False, description="Пропускать уже экспортированные"),
    output_dir: str = Form("export/stage-history", description="Папка вывода"),
    page_delay: float = Form(0.0, description="Пауза между страницами (сек)"),
    webhook_url: str | None = Security(_webhook_header),
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Экспортирует историю переходов по стадиям воронки для каждой сделки."""
    tid = _resolve_tenant_id(x_tenant_id)
    url = _resolve_webhook(webhook_url, tid)
    clean_funnels = [f for f in (funnel_id or []) if _none(f)] or None
    sink, mem = _tee()
    return _run_service(
        lambda: StageHistoryService(
            gateway=BitrixClient(url, page_delay=page_delay), sink=sink,
        ).execute(
            StageHistoryRequest(
                output_dir=Path(output_dir),
                category_ids=clean_funnels,
                deal_ids=deal_ids,
                date_from=_none(date_from),
                date_to=_none(date_to),
                responsible_id=_none(responsible_id),
                whatsapp_only=whatsapp_only,
                limit=limit,
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
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Экспорт WhatsApp через Open Lines API → ``export/whatsapp-timeline/``."""
    tid = _resolve_tenant_id(x_tenant_id)
    url = _resolve_whatsapp_webhook(webhook_url, tenant_id=tid)
    clean_funnels = [f for f in (funnel_id or []) if _none(f)] or None
    sink, mem = _tee()
    return _run_service(
        lambda: WhatsAppExportService(
            gateway=BitrixClient(url, page_delay=page_delay), sink=sink,
        ).execute(
            WhatsAppExportRequest(
                output_dir=Path("export/whatsapp-timeline"), limit=limit,
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
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Экспорт WhatsApp через timeline-комментарии (Wazzup-маркеры) → ``export/whatsapp-timeline/``."""
    tid = _resolve_tenant_id(x_tenant_id)
    url = _resolve_whatsapp_webhook(webhook_url, tenant_id=tid)
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
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Транскрипция аудиозаписей через OpenAI Whisper → ``export/transcripts/``."""
    tid = _resolve_tenant_id(x_tenant_id)
    key = _resolve_openai_key(openai_key, tid)
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
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Извлечение фич из транскриптов звонков через OpenAI → ``export/call-features/``."""
    tid = _resolve_tenant_id(x_tenant_id)
    key = _resolve_openai_key(openai_key, tid)
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
    """Агрегация feature-файлов в статистику по отделу продаж."""
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
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Генерация рекомендаций по всему отделу продаж на основе агрегированной статистики."""
    tid = _resolve_tenant_id(x_tenant_id)
    key = _resolve_openai_key(openai_key, tid)
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
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Извлечение фич из WhatsApp-переписок через OpenAI → ``export/whatsapp-features/``."""
    tid = _resolve_tenant_id(x_tenant_id)
    key = _resolve_openai_key(openai_key, tid)
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


@app.post("/sales-quality/analyze")
def analyze_sales_quality(
    call_transcript_manifest: str = Form(
        "export/recordings/transcripts/transcripts_manifest.json",
        description="Path to call transcripts manifest; use '-' to skip calls",
    ),
    call_metadata: str = Form(
        "export/call-records-scan/recording-candidates.json",
        description="Path to recording-candidates.json; use '-' if unavailable",
    ),
    activity_metadata: str = Form(
        "export/call-records-scan/activities.source.json",
        description="Path to call activities.source.json; use '-' if unavailable",
    ),
    whatsapp_conversation_dir: str = Form(
        "export/whatsapp-timeline/conversations_filtered",
        description="Directory with filtered WhatsApp conversations; use '-' to skip WhatsApp",
    ),
    users_path: str = Form("", description="Optional users.json for manager names"),
    output_dir: str = Form("export/sales-quality", description="Output directory"),
    model: str = Form("gpt-4o-mini", description="OpenAI model"),
    limit: int = Form(0, description="Max interactions (0 = all)"),
    skip_existing: bool = Form(False, description="Skip already processed interactions"),
    slow_response_threshold_sec: int = Form(
        900,
        description="Slow first-response threshold in seconds",
    ),
    max_chars_per_item: int = Form(
        24000,
        description="Max interaction text chars sent to OpenAI",
    ),
    openai_key: str | None = Security(_openai_key_header),
    x_tenant_id: str | None = Header(None),
) -> dict[str, Any]:
    """Analyze sales-quality signals across calls and WhatsApp."""
    tid = _resolve_tenant_id(x_tenant_id)
    key = _resolve_openai_key(openai_key, tid)
    sink, mem = _tee()
    return _run_service(
        lambda: AnalyzeSalesQualityService(
            gateway=OpenAiResponsesClient(key),
            sink=sink,
        ).execute(
            AnalyzeSalesQualityRequest(
                output_dir=Path(output_dir),
                call_transcript_manifest_path=Path(call_transcript_manifest)
                if _none(call_transcript_manifest) not in ("-", None)
                else None,
                call_metadata_path=Path(call_metadata)
                if _none(call_metadata) not in ("-", None)
                else None,
                activity_metadata_path=Path(activity_metadata)
                if _none(activity_metadata) not in ("-", None)
                else None,
                whatsapp_conversation_dir=Path(whatsapp_conversation_dir)
                if _none(whatsapp_conversation_dir) not in ("-", None)
                else None,
                users_path=Path(users_path) if _none(users_path) else None,
                model=model,
                limit=limit,
                skip_existing=skip_existing,
                slow_response_threshold_sec=slow_response_threshold_sec,
                max_chars_per_item=max_chars_per_item,
            )
        ),
        mem,
    )
