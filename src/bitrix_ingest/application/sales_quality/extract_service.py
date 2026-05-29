"""Analyze sales-quality signals across calls and WhatsApp conversations."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ...domain.openai_usage import extract_usage_event, summarize_usage
from ...domain.sales_quality import (
    PROBLEM_DEFINITIONS,
    SALES_STAGE_DEFINITIONS,
    sales_quality_schema,
)
from ..ports import JsonSink
from ..progress import ProgressCallback, emit_progress

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a strict sales quality auditor.

Analyze one sales interaction: either a phone-call transcript or a WhatsApp chat.
Return JSON only and follow the schema exactly.
Use only evidence from the provided interaction and metadata.
Do not invent budgets, deadlines, objections, prices, stages, or client intent.
All free-text fields must be in Russian.

Audit the following sales stages:
1. contact_established: manager greeted the client, introduced themself/company, or opened the conversation politely.
2. need_identified: manager asked enough questions to understand the client's need, product type, size, timing, context, or pain.
3. product_presented: manager explained a relevant product/service/solution, not just sent a generic greeting.
4. offer_or_usp_mentioned: manager mentioned a promotion, special offer, unique selling proposition, advantage, guarantee, showroom, delivery, measuring, installation, quality, portfolio, reviews, or any specific offer.
5. next_step_attempted / sale_attempted: manager tried to move the client forward, agree a call/visit/measurement/quote/follow-up, or directly close the sale.

Flag problems conservatively:
- missing_qualification=true if need/budget/timeline/decision-maker are mostly not clarified.
- missing_next_step=true if no concrete next action is proposed or agreed.
- weak_presentation=true if the presentation is generic, unclear, missing, or not tied to the client's need.
- not_target_lead=true if the request is outside the company's sales offer or the dialogue is not a real lead.
- manager_did_not_ask_questions=true if the manager did not meaningfully question the client.
- no_offer_or_usp=true if no promotion, USP, advantage, or concrete offer is mentioned.
- fragmented_or_low_content=true if the interaction is too short, one-sided, media-only, or lacks enough context.
- unclear_audio_or_text=true if the transcript/chat text is too unclear for reliable analysis.
- slow_response is provided in metadata when it can be calculated; keep it true only when metadata says it is slow.
"""


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
class AnalyzeSalesQualityRequest:
    output_dir: Path
    call_transcript_manifest_path: Path | None = Path(
        "export/recordings/transcripts/transcripts_manifest.json"
    )
    call_metadata_path: Path | None = Path(
        "export/call-records-scan/recording-candidates.json"
    )
    activity_metadata_path: Path | None = Path(
        "export/call-records-scan/activities.source.json"
    )
    whatsapp_conversation_dir: Path | None = Path(
        "export/whatsapp-timeline/conversations_filtered"
    )
    users_path: Path | None = None
    model: str = "gpt-4o-mini"
    limit: int = 0
    skip_existing: bool = False
    slow_response_threshold_sec: int = 900
    max_chars_per_item: int = 24000
    progress_callback: ProgressCallback | None = None


@dataclass(frozen=True)
class _Interaction:
    source_type: str
    source_id: str
    source_file_path: Path
    text: str
    manager_id: str = ""
    manager_name: str = ""
    deal_id: str = ""
    crm_activity_id: str = ""
    record_file_id: str = ""
    contact_id: str = ""
    channel: str = ""
    started_at: str = ""
    first_response_time_sec: int | None = None
    avg_response_latency_sec: int | None = None
    is_slow_response: bool | None = None
    metadata: dict[str, Any] | None = None


def _load_json_list(path: Path) -> list[Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], list):
        raw = raw[0]
    return raw if isinstance(raw, list) else [raw]


def _safe_part(value: str) -> str:
    safe = re.sub(r"\s+", "_", str(value or ""))
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", safe)
    safe = safe.strip("_")
    return safe or "unknown"


def _maybe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _maybe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(count: int, total: int) -> float:
    return round(count / total * 100, 1) if total else 0.0


def _clip_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    keep_head = max_chars // 2
    keep_tail = max_chars - keep_head
    omitted = len(text) - max_chars
    return (
        text[:keep_head]
        + f"\n\n[... omitted {omitted} chars ...]\n\n"
        + text[-keep_tail:]
    )


def _yn_is_yes(value: Any) -> bool:
    return str(value or "").lower() == "yes"


def _next_step_or_sale(stages: dict[str, Any]) -> bool:
    return _yn_is_yes(stages.get("next_step_attempted")) or _yn_is_yes(
        stages.get("sale_attempted")
    )


def _empty_problem_map() -> dict[str, bool]:
    return {item["key"]: False for item in PROBLEM_DEFINITIONS}


class AnalyzeSalesQualityService:
    def __init__(self, gateway: ResponsesGateway, sink: JsonSink) -> None:
        self._gateway = gateway
        self._sink = sink

    def execute(self, request: AnalyzeSalesQualityRequest) -> None:
        interactions = self._load_interactions(request)
        if request.limit > 0:
            interactions = interactions[: request.limit]
        if not interactions:
            raise ValueError("No call transcripts or WhatsApp conversations found.")

        output_dir = request.output_dir
        features_dir = output_dir / "features"
        raw_dir = output_dir / "raw"
        for directory in (output_dir, features_dir, raw_dir):
            directory.mkdir(parents=True, exist_ok=True)

        schema = sales_quality_schema()
        feature_files: list[Path] = []
        errors: list[dict[str, Any]] = []
        usage_events: list[dict[str, Any]] = []

        logger.info("Sales-quality analyzer: %d interaction(s)", len(interactions))

        total = len(interactions)
        emit_progress(
            request.progress_callback,
            current=0,
            total=total,
            message=f"Найдено взаимодействий для AI-оценки: {total}",
        )
        for index, item in enumerate(interactions, start=1):
            try:
                stem = self._feature_stem(item)
                feature_path = features_dir / f"{stem}.json"
                raw_path = raw_dir / f"{stem}.json"

                if request.skip_existing and feature_path.exists() and raw_path.exists():
                    feature_files.append(feature_path)
                    logger.info("Skipped existing sales-quality feature: %s", feature_path.name)
                    continue

                user_prompt = self._build_prompt(item, request.max_chars_per_item)
                try:
                    response = self._gateway.complete(
                        system_prompt=_SYSTEM_PROMPT,
                        user_prompt=user_prompt,
                        model=request.model,
                        schema_name="sales_quality_audit",
                        schema=schema,
                    )
                    parsed = json.loads(self._gateway.extract_output_text(response))
                    final_feature = self._normalize_feature(item, parsed, request)
                    self._sink.write(feature_path, final_feature)
                    self._sink.write(raw_path, response)
                    feature_files.append(feature_path)

                    event = extract_usage_event(
                        response,
                        stage="sales_quality_analysis",
                        entity_type=item.source_type,
                        entity_id=item.source_id,
                        source_file_path=str(item.source_file_path),
                        model=request.model,
                        extra={
                            "manager_id": item.manager_id,
                            "deal_id": item.deal_id,
                            "crm_activity_id": item.crm_activity_id,
                        },
                    )
                    if event:
                        usage_events.append(event)
                    logger.info("Analyzed %s %s", item.source_type, item.source_id)
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        {
                            "source_type": item.source_type,
                            "source_id": item.source_id,
                            "source_file_path": str(item.source_file_path),
                            "error": str(exc),
                        }
                    )
                    logger.warning(
                        "Failed sales-quality analysis for %s %s: %s",
                        item.source_type,
                        item.source_id,
                        exc,
                    )
            finally:
                emit_progress(
                    request.progress_callback,
                    current=index,
                    total=total,
                    message=f"AI оценивает коммуникации: {index} из {total}",
                )

        features = []
        for path in feature_files:
            try:
                features.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {
                        "source_type": "feature_file",
                        "source_id": path.stem,
                        "source_file_path": str(path),
                        "error": f"Feature reload failed: {exc}",
                    }
                )

        report = self._build_report(
            features,
            errors=errors,
            slow_response_threshold_sec=request.slow_response_threshold_sec,
        )
        self._sink.write(output_dir / "report.json", report)
        self._sink.write(output_dir / "errors.json", errors)
        self._sink.write(output_dir / "usage-events.json", usage_events)
        self._sink.write(output_dir / "usage-summary.json", summarize_usage(usage_events))
        (output_dir / "report.md").write_text(
            self._build_markdown_report(report),
            encoding="utf-8",
        )

        logger.info("Sales-quality analysis completed. Files saved to %s", output_dir)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load_interactions(self, request: AnalyzeSalesQualityRequest) -> list[_Interaction]:
        users = self._load_user_index(request.users_path)
        items: list[_Interaction] = []
        items.extend(self._load_call_interactions(request, users))
        items.extend(self._load_whatsapp_interactions(request, users))
        return items

    def _load_user_index(self, users_path: Path | None) -> dict[str, str]:
        if not users_path or not users_path.exists():
            return {}
        try:
            raw = json.loads(users_path.read_text(encoding="utf-8"))
            rows = raw.get("result") if isinstance(raw, dict) else raw
            if not isinstance(rows, list):
                rows = [rows]
        except Exception:  # noqa: BLE001
            return {}
        index: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            user_id = str(row.get("ID") or row.get("id") or "")
            if not user_id:
                continue
            name = " ".join(
                part
                for part in [
                    str(row.get("NAME") or row.get("name") or "").strip(),
                    str(row.get("LAST_NAME") or row.get("last_name") or "").strip(),
                ]
                if part
            )
            index[user_id] = name or user_id
        return index

    def _load_call_interactions(
        self,
        request: AnalyzeSalesQualityRequest,
        users: dict[str, str],
    ) -> list[_Interaction]:
        manifest_path = request.call_transcript_manifest_path
        if not manifest_path or not manifest_path.exists():
            return []

        call_meta = self._index_by(
            request.call_metadata_path,
            keys=("CRM_ACTIVITY_ID", "ID"),
        )
        activity_meta = self._index_by(
            request.activity_metadata_path,
            keys=("ID", "CRM_ACTIVITY_ID"),
        )
        interactions: list[_Interaction] = []
        for entry in _load_json_list(manifest_path):
            if not isinstance(entry, dict):
                continue
            status = str(
                entry.get("TRANSCRIPT_STATUS")
                or entry.get("STATUS")
                or ""
            )
            if status not in ("transcribed", "skipped_existing"):
                continue
            text_path_value = (
                entry.get("TRANSCRIPT_PATH")
                or entry.get("TEXT_FILE_PATH")
                or entry.get("text_file_path")
            )
            if not text_path_value:
                continue
            text_path = Path(str(text_path_value))
            if not text_path.exists():
                continue
            text = text_path.read_text(encoding="utf-8").strip()
            if not text:
                continue

            crm_id = str(entry.get("CRM_ACTIVITY_ID") or entry.get("crm_activity_id") or "")
            record_id = str(entry.get("RECORD_FILE_ID") or entry.get("record_file_id") or "")
            meta = call_meta.get(crm_id, {})
            activity = activity_meta.get(crm_id, {})
            manager_id = str(
                entry.get("RESPONSIBLE_ID")
                or meta.get("RESPONSIBLE_ID")
                or activity.get("RESPONSIBLE_ID")
                or ""
            )
            deal_id = ""
            owner_type = str(activity.get("OWNER_TYPE_ID") or meta.get("OWNER_TYPE_ID") or "")
            if owner_type == "2":
                deal_id = str(activity.get("OWNER_ID") or meta.get("OWNER_ID") or "")
            interactions.append(
                _Interaction(
                    source_type="call",
                    source_id=crm_id or record_id or self._hash_id(str(text_path)),
                    source_file_path=text_path,
                    text=text,
                    manager_id=manager_id,
                    manager_name=users.get(manager_id, ""),
                    deal_id=deal_id,
                    crm_activity_id=crm_id,
                    record_file_id=record_id,
                    channel="phone_call",
                    started_at=str(
                        entry.get("START_TIME")
                        or meta.get("START_TIME")
                        or activity.get("START_TIME")
                        or ""
                    ),
                    metadata={
                        "phone_number": str(
                            entry.get("PHONE_NUMBER") or meta.get("PHONE_NUMBER") or ""
                        ),
                        "call_duration_sec": _maybe_int(meta.get("CALL_DURATION")),
                        "record_duration_sec": _maybe_int(
                            entry.get("RECORD_DURATION") or meta.get("RECORD_DURATION")
                        ),
                        "direction": str(activity.get("DIRECTION") or ""),
                        "subject": str(activity.get("SUBJECT") or meta.get("SUBJECT") or ""),
                    },
                )
            )
        return interactions

    def _load_whatsapp_interactions(
        self,
        request: AnalyzeSalesQualityRequest,
        users: dict[str, str],
    ) -> list[_Interaction]:
        conversation_dir = request.whatsapp_conversation_dir
        if not conversation_dir or not conversation_dir.exists():
            return []

        interactions: list[_Interaction] = []
        for path in sorted(conversation_dir.glob("*.json")):
            try:
                conversation = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            text = self._conversation_to_text(conversation)
            if not text:
                continue
            stats = conversation.get("stats") or {}
            first_response = _maybe_int(stats.get("first_manager_response_time_sec"))
            avg_latency = _maybe_int(stats.get("avg_response_latency_sec"))
            slow_response: bool | None = None
            if first_response is not None:
                slow_response = first_response > request.slow_response_threshold_sec
            manager_id = str(conversation.get("assigned_by_id") or "")
            deal_id = str(conversation.get("deal_id") or "")
            interactions.append(
                _Interaction(
                    source_type="whatsapp",
                    source_id=deal_id or self._hash_id(str(path)),
                    source_file_path=path,
                    text=text,
                    manager_id=manager_id,
                    manager_name=users.get(manager_id, ""),
                    deal_id=deal_id,
                    contact_id=str(conversation.get("contact_id") or ""),
                    channel=str(conversation.get("channel") or "whatsapp"),
                    started_at=str(
                        stats.get("first_message_at")
                        or conversation.get("date_create")
                        or ""
                    ),
                    first_response_time_sec=first_response,
                    avg_response_latency_sec=avg_latency,
                    is_slow_response=slow_response,
                    metadata={
                        "stage_id": str(conversation.get("stage_id") or ""),
                        "stage_semantic_id": str(
                            conversation.get("stage_semantic_id") or ""
                        ),
                        "date_create": str(conversation.get("date_create") or ""),
                        "date_modify": str(conversation.get("date_modify") or ""),
                        "last_communication_time": str(
                            conversation.get("last_communication_time") or ""
                        ),
                        "total_messages": _maybe_int(stats.get("total_messages")),
                        "manager_messages": _maybe_int(stats.get("manager_messages")),
                        "client_messages": _maybe_int(stats.get("client_messages")),
                    },
                )
            )
        return interactions

    def _index_by(
        self,
        path: Path | None,
        *,
        keys: tuple[str, ...],
    ) -> dict[str, dict[str, Any]]:
        if not path or not path.exists():
            return {}
        index: dict[str, dict[str, Any]] = {}
        for row in _load_json_list(path):
            if not isinstance(row, dict):
                continue
            for key in keys:
                value = str(row.get(key) or "")
                if value:
                    index[value] = row
        return index

    def _conversation_to_text(self, conversation: dict[str, Any]) -> str:
        lines: list[str] = []
        for message in conversation.get("messages") or []:
            if not isinstance(message, dict):
                continue
            role = str(message.get("sender_role") or "unknown")
            created_at = str(message.get("created_at") or "")
            text = str(message.get("text") or "").strip()
            attachments = message.get("attachments") or []
            attachment_bits: list[str] = []
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                label = str(attachment.get("label") or attachment.get("type") or "file")
                transcript = str(
                    attachment.get("transcript")
                    or attachment.get("transcription")
                    or ""
                ).strip()
                if transcript:
                    attachment_bits.append(f"attachment {label}: {transcript}")
                else:
                    attachment_bits.append(f"attachment {label}")
            body = text
            if attachment_bits:
                body = "\n".join(part for part in [body, *attachment_bits] if part)
            if body:
                lines.append(f"[{created_at}] {role}: {body}")
        return "\n\n".join(lines).strip()

    # ------------------------------------------------------------------
    # Prompt and feature normalization
    # ------------------------------------------------------------------

    def _feature_stem(self, item: _Interaction) -> str:
        if item.source_type == "call":
            stem = f"call_activity_{_safe_part(item.crm_activity_id or item.source_id)}"
            if item.record_file_id:
                stem += f"__record_{_safe_part(item.record_file_id)}"
            return stem
        if item.source_type == "whatsapp":
            return f"whatsapp_deal_{_safe_part(item.deal_id or item.source_id)}"
        return f"{_safe_part(item.source_type)}_{_safe_part(item.source_id)}"

    def _build_prompt(self, item: _Interaction, max_chars: int) -> str:
        slow_label = "unknown"
        if item.is_slow_response is True:
            slow_label = "yes"
        elif item.is_slow_response is False:
            slow_label = "no"
        metadata = {
            "source_type": item.source_type,
            "source_id": item.source_id,
            "channel": item.channel,
            "manager_id": item.manager_id,
            "manager_name": item.manager_name,
            "deal_id": item.deal_id,
            "crm_activity_id": item.crm_activity_id,
            "record_file_id": item.record_file_id,
            "contact_id": item.contact_id,
            "started_at": item.started_at,
            "first_response_time_sec": item.first_response_time_sec,
            "avg_response_latency_sec": item.avg_response_latency_sec,
            "slow_response_by_rule": slow_label,
            **(item.metadata or {}),
        }
        return (
            "Metadata:\n"
            f"{json.dumps(metadata, ensure_ascii=False, indent=2)}\n\n"
            "Interaction text:\n"
            f"{_clip_text(item.text, max_chars)}"
        )

    def _normalize_feature(
        self,
        item: _Interaction,
        parsed: dict[str, Any],
        request: AnalyzeSalesQualityRequest,
    ) -> dict[str, Any]:
        stages = dict(parsed.get("sales_stages") or {})
        next_step_or_sale = _next_step_or_sale(stages)

        problems = _empty_problem_map()
        problems.update(
            {
                key: bool(value)
                for key, value in (parsed.get("problems") or {}).items()
                if key in problems
            }
        )
        if item.is_slow_response is not None:
            problems["slow_response"] = item.is_slow_response
        lead_quality = parsed.get("lead_quality") or {}
        if str(lead_quality.get("status") or "") == "not_target":
            problems["not_target_lead"] = True
        if not next_step_or_sale and str((parsed.get("next_step") or {}).get("status") or "") in (
            "none",
            "unclear",
            "",
        ):
            problems["missing_next_step"] = True

        stage_flags = {
            "contact_established": _yn_is_yes(stages.get("contact_established")),
            "need_identified": _yn_is_yes(stages.get("need_identified")),
            "product_presented": _yn_is_yes(stages.get("product_presented")),
            "offer_or_usp_mentioned": _yn_is_yes(stages.get("offer_or_usp_mentioned")),
            "next_step_or_sale": next_step_or_sale,
        }
        stage_score_pct = round(sum(stage_flags.values()) / len(stage_flags) * 100, 1)

        return {
            "schema_version": "sales_quality_v1",
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "source": {
                "source_type": item.source_type,
                "source_id": item.source_id,
                "source_file_path": str(item.source_file_path),
                "channel": item.channel,
                "manager_id": item.manager_id,
                "manager_name": item.manager_name,
                "deal_id": item.deal_id,
                "crm_activity_id": item.crm_activity_id,
                "record_file_id": item.record_file_id,
                "contact_id": item.contact_id,
                "started_at": item.started_at,
            },
            "response_time": {
                "first_response_time_sec": item.first_response_time_sec,
                "avg_response_latency_sec": item.avg_response_latency_sec,
                "slow_response_threshold_sec": request.slow_response_threshold_sec,
                "is_slow_response": item.is_slow_response,
            },
            **{
                key: value
                for key, value in parsed.items()
                if key not in ("problems",)
            },
            "problems": problems,
            "stage_flags": stage_flags,
            "stage_score_pct": stage_score_pct,
        }

    def _hash_id(self, text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _build_report(
        self,
        features: list[dict[str, Any]],
        *,
        errors: list[dict[str, Any]],
        slow_response_threshold_sec: int,
    ) -> dict[str, Any]:
        total = len(features)
        by_manager: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_channel: Counter[str] = Counter()
        for feature in features:
            source = feature.get("source") or {}
            manager_id = str(source.get("manager_id") or "unknown")
            by_manager[manager_id].append(feature)
            by_channel[str(source.get("source_type") or "unknown")] += 1

        stage_funnel = self._stage_rows(features)
        problem_rows = self._problem_rows(features)
        per_manager = []
        for manager_id, manager_features in sorted(by_manager.items()):
            source = manager_features[0].get("source") or {}
            per_manager.append(
                {
                    "manager_id": manager_id,
                    "manager_name": str(source.get("manager_name") or ""),
                    "total": len(manager_features),
                    "overall_stage_score_pct": self._avg_stage_score(manager_features),
                    "stage_compliance": self._stage_rows(manager_features),
                    "problems": self._problem_rows(manager_features),
                    "response_time": self._response_time_summary(manager_features),
                }
            )

        return {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "total_interactions": total,
            "errors_count": len(errors),
            "slow_response_threshold_sec": slow_response_threshold_sec,
            "by_channel": dict(sorted(by_channel.items())),
            "overall_stage_score_pct": self._avg_stage_score(features),
            "stage_funnel": stage_funnel,
            "top_problems": problem_rows[:10],
            "per_manager": per_manager,
            "visuals": {
                "stage_funnel": stage_funnel,
                "manager_heatmap": self._manager_heatmap(per_manager),
                "problem_bar_chart": problem_rows,
            },
        }

    def _stage_rows(self, features: list[dict[str, Any]]) -> list[dict[str, Any]]:
        total = len(features)
        rows = []
        for definition in SALES_STAGE_DEFINITIONS:
            key = definition["key"]
            yes_count = sum(
                1
                for feature in features
                if bool((feature.get("stage_flags") or {}).get(key))
            )
            rows.append(
                {
                    "key": key,
                    "label": definition["label"],
                    "yes_count": yes_count,
                    "total": total,
                    "pct": _pct(yes_count, total),
                }
            )
        return rows

    def _problem_rows(self, features: list[dict[str, Any]]) -> list[dict[str, Any]]:
        total = len(features)
        rows = []
        for definition in PROBLEM_DEFINITIONS:
            key = definition["key"]
            count = sum(
                1
                for feature in features
                if bool((feature.get("problems") or {}).get(key))
            )
            rows.append(
                {
                    "key": key,
                    "label": definition["label"],
                    "count": count,
                    "total": total,
                    "pct": _pct(count, total),
                }
            )
        return sorted(rows, key=lambda row: (-row["count"], row["label"]))

    def _avg_stage_score(self, features: list[dict[str, Any]]) -> float:
        if not features:
            return 0.0
        values = [_maybe_float(feature.get("stage_score_pct")) or 0.0 for feature in features]
        return round(sum(values) / len(values), 1)

    def _response_time_summary(self, features: list[dict[str, Any]]) -> dict[str, Any]:
        values = [
            _maybe_int((feature.get("response_time") or {}).get("first_response_time_sec"))
            for feature in features
        ]
        known = [value for value in values if value is not None]
        slow = sum(
            1
            for feature in features
            if (feature.get("response_time") or {}).get("is_slow_response") is True
        )
        return {
            "known_count": len(known),
            "avg_first_response_time_sec": round(sum(known) / len(known), 1)
            if known
            else None,
            "slow_count": slow,
            "slow_pct": _pct(slow, len(known)),
        }

    def _manager_heatmap(self, per_manager: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = []
        for manager in per_manager:
            stage_values = {
                row["key"]: row["pct"]
                for row in manager.get("stage_compliance") or []
            }
            rows.append(
                {
                    "manager_id": manager["manager_id"],
                    "manager_name": manager.get("manager_name", ""),
                    "total": manager["total"],
                    "overall_stage_score_pct": manager["overall_stage_score_pct"],
                    **stage_values,
                }
            )
        return rows

    def _build_markdown_report(self, report: dict[str, Any]) -> str:
        lines = [
            "# Отчет по качеству продаж",
            "",
            f"Всего взаимодействий: {report.get('total_interactions', 0)}",
            f"Среднее соблюдение этапов: {report.get('overall_stage_score_pct', 0)}%",
            "",
            "## Воронка этапов",
            "",
            "| Этап | % | Кол-во |",
            "|---|---:|---:|",
        ]
        for row in report.get("stage_funnel") or []:
            lines.append(
                f"| {row['label']} | {row['pct']}% | {row['yes_count']}/{row['total']} |"
            )
        lines.extend(
            [
                "",
                "## Топ проблем",
                "",
                "| Проблема | % | Кол-во |",
                "|---|---:|---:|",
            ]
        )
        for row in report.get("top_problems") or []:
            if row.get("count", 0) <= 0:
                continue
            lines.append(
                f"| {row['label']} | {row['pct']}% | {row['count']}/{row['total']} |"
            )
        lines.extend(
            [
                "",
                "## Менеджеры",
                "",
                "| Менеджер | Всего | Этапы | Долгий ответ |",
                "|---|---:|---:|---:|",
            ]
        )
        for row in report.get("per_manager") or []:
            manager_label = row.get("manager_name") or row.get("manager_id")
            response = row.get("response_time") or {}
            lines.append(
                f"| {manager_label} | {row['total']} | "
                f"{row['overall_stage_score_pct']}% | {response.get('slow_pct')}% |"
            )
        lines.append("")
        return "\n".join(lines)
