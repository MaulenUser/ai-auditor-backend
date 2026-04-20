"""WhatsAppTimelineExportService — timeline-comment based WhatsApp export.

Port of bitrix-export-whatsapp-timeline.ps1.
Uses crm.timeline.comment.list (Wazzup comment markers) instead of Open Lines API.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...domain.whatsapp import TimelineSource
from ..date_range import within_any_record_datetime_range
from ..ports import BitrixGateway, JsonSink
from ..whatsapp.conversation_assembler import ConversationAssembler
from ..whatsapp.deal_filter import WhatsAppDealFilter

logger = logging.getLogger(__name__)

_DEAL_SELECT = [
    "ID", "TITLE", "CONTACT_ID", "SOURCE_ID", "ASSIGNED_BY_ID",
    "STAGE_ID", "CATEGORY_ID", "DATE_CREATE", "DATE_MODIFY", "LAST_COMMUNICATION_TIME",
]
_TIMELINE_SELECT = ["ID", "CREATED", "ENTITY_ID", "ENTITY_TYPE", "AUTHOR_ID", "COMMENT", "FILES"]


@dataclass(frozen=True)
class WhatsAppTimelineExportRequest:
    output_dir: Path
    limit: int = 100
    date_from: str | None = None
    date_to: str | None = None
    deal_ids: list[str] | None = None
    skip_existing: bool = False
    category_ids: list[str] | None = None  # None = all; list = OR across funnels
    responsible_id: str | None = None


class WhatsAppTimelineExportService:
    def __init__(
        self,
        gateway: BitrixGateway,
        sink: JsonSink,
        *,
        deal_filter: WhatsAppDealFilter | None = None,
        assembler: ConversationAssembler | None = None,
    ) -> None:
        self._gateway = gateway
        self._sink = sink
        self._deal_filter = deal_filter or WhatsAppDealFilter()
        self._assembler = assembler or ConversationAssembler()

    def execute(self, request: WhatsAppTimelineExportRequest) -> None:
        raw_dir = request.output_dir / "raw"
        conversations_dir = request.output_dir / "conversations"
        for d in (request.output_dir, raw_dir, conversations_dir):
            d.mkdir(parents=True, exist_ok=True)

        profile = self._gateway.call("profile")
        result = profile.get("result") or {}
        logger.info(
            "Connected as user ID %s: %s %s",
            result.get("ID"), result.get("NAME"), result.get("LAST_NAME"),
        )
        self._sink.write(request.output_dir / "profile.json", profile)

        deals = self._load_whatsapp_deals(request)
        self._sink.write(request.output_dir / "deals.source.json", deals)

        errors: list[dict[str, Any]] = []
        report_rows: list[dict[str, Any]] = []
        totals: dict[str, int] = {
            "deals_scanned": len(deals),
            "chats_exported": 0,
            "skipped_existing": 0,
            "total_messages": 0,
            "manager_messages": 0,
            "client_messages": 0,
            "system_messages": 0,
            "messages_with_files": 0,
        }

        for deal in deals:
            deal_id = str(deal.get("ID", ""))
            conversation_path = conversations_dir / f"deal_{deal_id}.json"

            if request.skip_existing and conversation_path.exists():
                totals["skipped_existing"] += 1
                logger.info("Skipped deal ID=%s (conversation already exists)", deal_id)
                continue

            try:
                conversation = self._process_deal(
                    deal, deal_id,
                    raw_dir / f"deal_{deal_id}.timeline.json",
                    raw_dir / f"deal_{deal_id}.contact.timeline.json",
                )
                self._sink.write(conversation_path, conversation.to_dict())

                stats = conversation.stats
                totals["chats_exported"] += 1
                totals["total_messages"] += stats.total_messages
                totals["manager_messages"] += stats.manager_messages
                totals["client_messages"] += stats.client_messages
                totals["system_messages"] += stats.system_messages
                totals["messages_with_files"] += stats.messages_with_files

                report_rows.append({
                    "deal_id": deal_id,
                    "deal_title": str(deal.get("TITLE", "")),
                    "contact_id": str(deal.get("CONTACT_ID", "")),
                    "source_id": str(deal.get("SOURCE_ID", "")),
                    "total_messages": stats.total_messages,
                    "manager_messages": stats.manager_messages,
                    "client_messages": stats.client_messages,
                    "system_messages": stats.system_messages,
                    "messages_with_files": stats.messages_with_files,
                    "first_message_at": stats.first_message_at,
                    "last_message_at": stats.last_message_at,
                    "timeline_source": conversation.timeline_source,
                    "timeline_entity_type": conversation.timeline_entity_type,
                    "timeline_entity_id": conversation.timeline_entity_id,
                    "output_file": str(conversation_path),
                })
                logger.info(
                    "Exported timeline for deal ID=%s from %s: %d messages",
                    deal_id, conversation.timeline_source, stats.total_messages,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({"deal_id": deal_id, "message": str(exc)})
                logger.warning("Failed deal ID=%s: %s", deal_id, exc)

        report = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "totals": totals,
            "rows": report_rows,
            "errors_count": len(errors),
        }
        self._sink.write(request.output_dir / "report.json", report)
        self._sink.write(request.output_dir / "errors.json", errors)

        logger.info("WhatsApp timeline export completed.")
        logger.info("Deals scanned: %d", totals["deals_scanned"])
        logger.info("Chats exported: %d", totals["chats_exported"])
        logger.info("Messages exported: %d", totals["total_messages"])
        logger.info("Errors: %d", len(errors))
        logger.info("Files saved to %s", request.output_dir.resolve())

    def _load_whatsapp_deals(
        self, request: WhatsAppTimelineExportRequest
    ) -> list[dict[str, Any]]:
        deal_filter: dict[str, Any] = {}
        clean = [f for f in (request.category_ids or []) if f]
        if clean:
            deal_filter["CATEGORY_ID"] = clean if len(clean) > 1 else clean[0]
        if request.responsible_id:
            deal_filter["ASSIGNED_BY_ID"] = request.responsible_id
        all_deals = self._gateway.list_all(
            "crm.deal.list",
            select=_DEAL_SELECT,
            filter=deal_filter,
            order={"DATE_MODIFY": "DESC"},
            context="deals source",
        )
        all_deals = [
            deal for deal in all_deals
            if within_any_record_datetime_range(
                deal,
                fields=("DATE_CREATE", "DATE_MODIFY"),
                date_from=request.date_from,
                date_to=request.date_to,
            )
        ]
        whatsapp = self._deal_filter.select_whatsapp_deals(all_deals)
        if request.deal_ids:
            whatsapp = self._deal_filter.restrict_to_allowlist(whatsapp, request.deal_ids)
        whatsapp = self._deal_filter.sort_by_modified_desc(whatsapp)
        whatsapp = self._deal_filter.apply_limit(whatsapp, request.limit)
        logger.info("WhatsApp deals selected: %d", len(whatsapp))
        return whatsapp

    def _process_deal(
        self,
        deal: dict[str, Any],
        deal_id: str,
        deal_raw_path: Path,
        contact_raw_path: Path,
    ) -> Any:
        deal_timeline = self._gateway.list_all(
            "crm.timeline.comment.list",
            select=_TIMELINE_SELECT,
            filter={"ENTITY_ID": int(deal_id), "ENTITY_TYPE": "deal"},
            order={"CREATED": "ASC"},
            context=f"deal ID={deal_id} timeline",
        )
        self._sink.write(deal_raw_path, deal_timeline)

        conversation = self._assembler.assemble(
            deal=deal,
            timeline_comments=deal_timeline,
            timeline_source=TimelineSource.DEAL,
            timeline_entity_type="deal",
            timeline_entity_id=deal_id,
        )

        contact_id = str(deal.get("CONTACT_ID") or "")
        has_contact = bool(contact_id) and contact_id != "0"
        if conversation.stats.total_messages == 0 and has_contact:
            logger.info(
                "No WhatsApp messages in deal timeline for ID=%s. Trying contact ID=%s",
                deal_id, contact_id,
            )
            contact_timeline = self._gateway.list_all(
                "crm.timeline.comment.list",
                select=_TIMELINE_SELECT,
                filter={"ENTITY_ID": int(contact_id), "ENTITY_TYPE": "contact"},
                order={"CREATED": "ASC"},
                context=f"deal ID={deal_id} contact ID={contact_id} timeline",
            )
            self._sink.write(contact_raw_path, contact_timeline)
            contact_conv = self._assembler.assemble(
                deal=deal,
                timeline_comments=contact_timeline,
                timeline_source=TimelineSource.CONTACT,
                timeline_entity_type="contact",
                timeline_entity_id=contact_id,
            )
            if contact_conv.stats.total_messages > 0:
                logger.info(
                    "Using contact timeline for deal ID=%s: %d messages",
                    deal_id, contact_conv.stats.total_messages,
                )
                return contact_conv

        return conversation
