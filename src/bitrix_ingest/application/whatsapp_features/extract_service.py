"""ExtractWhatsAppFeaturesService — port of extract-whatsapp-features-openai.ps1."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ...domain.features import WhatsAppFeatureReportRow, whatsapp_features_schema
from ...domain.openai_usage import extract_usage_event, summarize_usage
from ..ports import JsonSink

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You analyze WhatsApp sales chats for an MVP analytics pipeline.

Return JSON only and follow the schema exactly.
Use only evidence from the chat and provided metadata.
If something is not clear from the chat, use "unknown".
Do not invent prices, budgets, timelines, intentions, or next steps.
Free-text fields must be in Russian.
Tags must be short Russian labels.

Interpretation rules:
- target_client: the chat is relevant to the company's target services.
- not_target_client: the client request is outside the company's target service, or the conversation is not a real lead conversation.
- technical_or_short: the chat is too short, too fragmented, or too poor in content for meaningful sales analysis.
- callback_requested: the client explicitly asks to continue via phone call or asks to be called back.
- awaiting_response: the dialogue is unfinished and a side is clearly waiting for the next reply or action.
- follow_up: there is a clear next step but not enough evidence for strong qualified interest.
- qualified_interest: the client shows meaningful service interest and the chat has real sales value.
- non_sales_chat=true when the chat is administrative, technical, wrong-thread, or otherwise not a real sales conversation.
- fragmented_chat=true when the dialogue is incomplete, one-sided, mostly media/system notices, or lacks enough back-and-forth context."""


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
class ExtractWhatsAppFeaturesRequest:
    output_dir: Path
    conversation_report_path: Path | None = None
    conversation_dir: Path | None = None
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


def _conversation_to_prompt_text(conversation: dict[str, Any]) -> str:
    lines: list[str] = []
    for msg in conversation.get("messages") or []:
        role = str(msg.get("sender_role") or "unknown")
        created_at = str(msg.get("created_at") or "")
        text = str(msg.get("text") or "")
        parts = [f"[{created_at}] {role}: {text}"]
        for att in msg.get("attachments") or []:
            parts.append(
                f"attachment: type={att.get('type')}; "
                f"label={att.get('label')}; url={att.get('url')}"
            )
        lines.append("\n".join(parts))
    return "\n\n".join(lines).strip()


def _load_entries(
    report_path: Path | None, conversation_dir: Path | None
) -> list[dict[str, Any]]:
    """Load conversation entry pointers from the report file, falling back to the directory."""
    if report_path and report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        rows = report.get("rows") or []
        if rows:
            return [
                {
                    "deal_id": str(row.get("deal_id") or ""),
                    "contact_id": str(row.get("contact_id") or ""),
                    "total_messages": int(row.get("total_messages") or 0),
                    "conversation_path": str(row.get("output_file") or ""),
                }
                for row in rows
            ]

    if not conversation_dir or not conversation_dir.exists():
        raise FileNotFoundError(
            "Neither conversation report nor conversation directory was found."
        )
    entries: list[dict[str, Any]] = []
    for json_file in sorted(conversation_dir.glob("*.json")):
        conv = json.loads(json_file.read_text(encoding="utf-8"))
        entries.append({
            "deal_id": str(conv.get("deal_id") or ""),
            "contact_id": str(conv.get("contact_id") or ""),
            "total_messages": int((conv.get("stats") or {}).get("total_messages") or 0),
            "conversation_path": str(json_file),
        })
    return entries


class ExtractWhatsAppFeaturesService:
    def __init__(self, gateway: ResponsesGateway, sink: JsonSink) -> None:
        self._gateway = gateway
        self._sink = sink

    def execute(self, request: ExtractWhatsAppFeaturesRequest) -> None:
        entries = _load_entries(request.conversation_report_path, request.conversation_dir)
        entries = [e for e in entries if str(e.get("conversation_path") or "").strip()]
        if request.limit > 0:
            entries = entries[: request.limit]
        if not entries:
            raise ValueError("No WhatsApp conversations available for feature extraction.")

        features_dir = request.output_dir / "features"
        raw_dir = request.output_dir / "raw"
        for d in (request.output_dir, features_dir, raw_dir):
            d.mkdir(parents=True, exist_ok=True)

        report_rows: list[WhatsAppFeatureReportRow] = []
        errors: list[dict[str, Any]] = []
        usage_events: list[dict[str, Any]] = []
        schema = whatsapp_features_schema()

        for entry in entries:
            deal_id = str(entry.get("deal_id") or "")
            contact_id = str(entry.get("contact_id") or "")
            conv_path = Path(str(entry.get("conversation_path") or ""))

            feature_path = features_dir / f"deal_{_safe_part(deal_id)}.json"
            raw_path = raw_dir / f"deal_{_safe_part(deal_id)}.json"

            if not conv_path.exists():
                errors.append({
                    "DEAL_ID": deal_id, "CONTACT_ID": contact_id,
                    "Error": "Conversation file not found.",
                })
                logger.info("Missing conversation for deal ID=%s", deal_id)
                continue

            if request.skip_existing and feature_path.exists() and raw_path.exists():
                existing = json.loads(feature_path.read_text(encoding="utf-8"))
                report_rows.append(WhatsAppFeatureReportRow(
                    deal_id=deal_id, contact_id=contact_id,
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
                logger.info("Skipped existing features for deal ID=%s", deal_id)
                continue

            conversation = json.loads(conv_path.read_text(encoding="utf-8"))
            conv_text = _conversation_to_prompt_text(conversation)
            if not conv_text:
                errors.append({
                    "DEAL_ID": deal_id, "CONTACT_ID": contact_id,
                    "Error": "Conversation text is empty after formatting.",
                })
                logger.info("Empty conversation for deal ID=%s", deal_id)
                continue

            stats = conversation.get("stats") or {}
            user_prompt = (
                f"Metadata:\n"
                f"- Deal ID: {conversation.get('deal_id', '')}\n"
                f"- Contact ID: {conversation.get('contact_id', '')}\n"
                f"- Source ID: {conversation.get('source_id', '')}\n"
                f"- Stage ID: {conversation.get('stage_id', '')}\n"
                f"- Assigned manager ID: {conversation.get('assigned_by_id', '')}\n"
                f"- Created at: {conversation.get('date_create', '')}\n"
                f"- Updated at: {conversation.get('date_modify', '')}\n"
                f"- Total messages: {stats.get('total_messages', 0)}\n"
                f"- Manager messages: {stats.get('manager_messages', 0)}\n"
                f"- Client messages: {stats.get('client_messages', 0)}\n"
                f"- System messages: {stats.get('system_messages', 0)}\n\n"
                f"Chat:\n{conv_text}"
            )

            try:
                response = self._gateway.complete(
                    system_prompt=_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    model=request.model,
                    schema_name="whatsapp_chat_features",
                    schema=schema,
                )
                parsed = json.loads(self._gateway.extract_output_text(response))
                event = extract_usage_event(
                    response,
                    stage="whatsapp_feature_extraction",
                    entity_type="deal",
                    entity_id=deal_id,
                    source_file_path=str(conv_path),
                    model=request.model,
                    extra={"contact_id": contact_id},
                )
                if event:
                    usage_events.append(event)

                final_feature = {
                    "schema_version": "mvp_whatsapp_features_v1",
                    "source": {
                        "channel": str(conversation.get("channel") or ""),
                        "integration": str(conversation.get("integration") or ""),
                        "deal_id": str(conversation.get("deal_id") or ""),
                        "contact_id": str(conversation.get("contact_id") or ""),
                        "source_id": str(conversation.get("source_id") or ""),
                        "assigned_by_id": str(conversation.get("assigned_by_id") or ""),
                        "stage_id": str(conversation.get("stage_id") or ""),
                        "category_id": str(conversation.get("category_id") or ""),
                        "date_create": str(conversation.get("date_create") or ""),
                        "date_modify": str(conversation.get("date_modify") or ""),
                        "last_communication_time": str(
                            conversation.get("last_communication_time") or ""
                        ),
                        "total_messages": int(stats.get("total_messages") or 0),
                        "manager_messages": int(stats.get("manager_messages") or 0),
                        "client_messages": int(stats.get("client_messages") or 0),
                        "system_messages": int(stats.get("system_messages") or 0),
                        "messages_with_files": int(stats.get("messages_with_files") or 0),
                        "conversation_file_path": str(conv_path),
                    },
                    **parsed,
                }
                self._sink.write(feature_path, final_feature)
                self._sink.write(raw_path, response)

                report_rows.append(WhatsAppFeatureReportRow(
                    deal_id=deal_id, contact_id=contact_id,
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
                logger.info("Extracted WhatsApp features for deal ID=%s", deal_id)
            except Exception as exc:  # noqa: BLE001
                errors.append({"DEAL_ID": deal_id, "CONTACT_ID": contact_id, "Error": str(exc)})
                logger.warning(
                    "Failed WhatsApp feature extraction for deal ID=%s: %s", deal_id, exc
                )

        self._sink.write(
            request.output_dir / "feature-report.json", [r.to_dict() for r in report_rows]
        )
        self._sink.write(request.output_dir / "errors.json", errors)
        self._sink.write(request.output_dir / "usage-events.json", usage_events)
        self._sink.write(request.output_dir / "usage-summary.json", summarize_usage(usage_events))

        logger.info("WhatsApp feature extraction completed.")
        logger.info("Extracted or skipped: %d", len(report_rows))
        logger.info("Errors: %d", len(errors))
        logger.info("Files saved to %s", request.output_dir.resolve())
