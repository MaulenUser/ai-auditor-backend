"""WhatsAppExportService - end-to-end WhatsApp Open Lines export."""
from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...domain.whatsapp import (
    ConversationStats,
    SenderRole,
    WhatsAppAttachment,
    WhatsAppConversation,
    WhatsAppMessage,
)
from ..date_range import build_closed_filter, within_datetime_range
from ..ports import BitrixGateway, JsonSink
from .bbcode import BBCodeStripper
from .deal_filter import WhatsAppDealFilter
from .text import WhitespaceNormalizer

logger = logging.getLogger(__name__)


_DEAL_SELECT: list[str] = [
    "ID",
    "TITLE",
    "CONTACT_ID",
    "SOURCE_ID",
    "ASSIGNED_BY_ID",
    "STAGE_ID",
    "CATEGORY_ID",
    "DATE_CREATE",
    "DATE_MODIFY",
    "LAST_COMMUNICATION_TIME",
]
_DEAL_ORDER: dict[str, str] = {"DATE_MODIFY": "DESC"}
_WHATSAPP_CONNECTOR = re.compile(r"whatsapp|wazzup", re.IGNORECASE)
_PLACEHOLDER_LABEL = re.compile(r"^[\s.\-]+$")
_GENERIC_BBCODE_TAG = re.compile(r"\[/?[a-z]+(?:=[^\]]+)?(?: [^\]]+)?\]", re.IGNORECASE)
_OUTGOING_MARKER = re.compile(
    r"^===\s*(?:Исходящее сообщение|Outgoing message).*?===\s*(?:\n|$)",
    re.IGNORECASE,
)
_INCOMING_MARKER = re.compile(
    r"^===\s*(?:Входящее сообщение|Incoming message).*?===\s*(?:\n|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WhatsAppExportRequest:
    """Request DTO for :class:`WhatsAppExportService`."""

    output_dir: Path
    limit: int = 100
    date_from: str | None = None
    date_to: str | None = None
    deal_ids: list[str] | None = None
    skip_existing: bool = False
    include_system_messages: bool = True


@dataclass
class _ExportAccumulator:
    """Internal state collected while iterating over deals."""

    errors: list[dict[str, Any]]
    report_rows: list[dict[str, Any]]
    totals: dict[str, int]

    @classmethod
    def for_deals(cls, deals_scanned: int) -> "_ExportAccumulator":
        return cls(
            errors=[],
            report_rows=[],
            totals={
                "deals_scanned": deals_scanned,
                "chats_exported": 0,
                "skipped_existing": 0,
                "total_messages": 0,
                "manager_messages": 0,
                "client_messages": 0,
                "system_messages": 0,
                "messages_with_files": 0,
            },
        )

    def record_conversation(
        self,
        deal: dict[str, Any],
        conversation: WhatsAppConversation,
        output_file: Path,
    ) -> None:
        stats = conversation.stats
        self.totals["chats_exported"] += 1
        self.totals["total_messages"] += stats.total_messages
        self.totals["manager_messages"] += stats.manager_messages
        self.totals["client_messages"] += stats.client_messages
        self.totals["system_messages"] += stats.system_messages
        self.totals["messages_with_files"] += stats.messages_with_files
        self.report_rows.append(
            {
                "deal_id": str(deal.get("ID", "")),
                "deal_title": str(deal.get("TITLE", "")),
                "contact_id": str(deal.get("CONTACT_ID", "")),
                "source_id": str(deal.get("SOURCE_ID", "")),
                "chat_id": conversation.chat_id,
                "session_id": conversation.session_id,
                "dialog_id": conversation.dialog_id,
                "connector_id": conversation.connector_id,
                "connector_title": conversation.connector_title,
                "chat_name": conversation.chat_name,
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
                "output_file": str(output_file),
            }
        )


@dataclass(frozen=True)
class _OpenLineBinding:
    timeline_source: str
    timeline_entity_type: str
    timeline_entity_id: str
    chat_id: str
    connector_id: str
    connector_title: str

    def to_dict(self) -> dict[str, str]:
        return {
            "timeline_source": self.timeline_source,
            "timeline_entity_type": self.timeline_entity_type,
            "timeline_entity_id": self.timeline_entity_id,
            "chat_id": self.chat_id,
            "connector_id": self.connector_id,
            "connector_title": self.connector_title,
        }


class WhatsAppExportService:
    """Orchestrates the full WhatsApp export flow through Open Lines APIs."""

    CHANNEL = "whatsapp"

    def __init__(
        self,
        gateway: BitrixGateway,
        sink: JsonSink,
        *,
        deal_filter: WhatsAppDealFilter | None = None,
        whitespace: WhitespaceNormalizer | None = None,
        bbcode_stripper: BBCodeStripper | None = None,
    ) -> None:
        self._gateway = gateway
        self._sink = sink
        self._deal_filter = deal_filter or WhatsAppDealFilter()
        self._whitespace = whitespace or WhitespaceNormalizer()
        self._bbcode_stripper = bbcode_stripper or BBCodeStripper()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, request: WhatsAppExportRequest) -> None:
        directories = _OutputDirectories.prepare(request.output_dir)

        self._export_profile(directories.root)
        deals = self._load_whatsapp_deals(request, directories.root)

        accumulator = _ExportAccumulator.for_deals(deals_scanned=len(deals))
        for deal in deals:
            self._process_deal(deal, request, directories, accumulator)

        self._write_reports(directories.root, accumulator)
        self._log_summary(directories.root, accumulator)

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _export_profile(self, output_dir: Path) -> None:
        profile = self._gateway.call("profile")
        result = profile.get("result") or {}
        logger.info(
            "Connected as user ID %s: %s %s",
            result.get("ID"),
            result.get("NAME"),
            result.get("LAST_NAME"),
        )
        self._sink.write(output_dir / "profile.json", profile)

    def _load_whatsapp_deals(
        self,
        request: WhatsAppExportRequest,
        output_dir: Path,
    ) -> list[dict[str, Any]]:
        deal_filter = build_closed_filter(
            "DATE_MODIFY",
            date_from=request.date_from,
            date_to=request.date_to,
        )

        whatsapp, raw_deals_scanned, whatsapp_matches = self._scan_whatsapp_deals(
            request=request,
            deal_filter=deal_filter,
        )
        logger.info("Deals scanned before selection stop: %d", raw_deals_scanned)
        logger.info("WhatsApp deals matched while scanning: %d", whatsapp_matches)
        if request.deal_ids:
            logger.info("WhatsApp deals after --deal-ids filter: %d", len(whatsapp))

        whatsapp = self._deal_filter.sort_by_modified_desc(whatsapp)
        whatsapp = self._deal_filter.apply_limit(whatsapp, request.limit)

        self._sink.write(output_dir / "deals.source.json", whatsapp)
        logger.info(
            "WhatsApp deals selected (after limit=%d): %d",
            request.limit,
            len(whatsapp),
        )
        return whatsapp

    def _scan_whatsapp_deals(
        self,
        *,
        request: WhatsAppExportRequest,
        deal_filter: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], int, int]:
        start = 0
        page_num = 1
        raw_deals_scanned = 0
        whatsapp_matches = 0
        selected: list[dict[str, Any]] = []
        page_delay = float(getattr(self._gateway, "page_delay", 0.0) or 0.0)

        while True:
            response = self._gateway.call(
                "crm.deal.list",
                body={
                    "select": _DEAL_SELECT,
                    "filter": deal_filter,
                    "order": _DEAL_ORDER,
                    "start": start,
                },
                label=f"crm.deal.list deals source page {page_num} (start={start})",
            )

            result = response.get("result")
            if result is None:
                return selected, raw_deals_scanned, whatsapp_matches

            rows: list[dict[str, Any]] = result if isinstance(result, list) else [result]
            raw_deals_scanned += len(rows)

            page_whatsapp = self._deal_filter.select_whatsapp_deals(rows)
            whatsapp_matches += len(page_whatsapp)

            if request.deal_ids:
                page_whatsapp = self._deal_filter.restrict_to_allowlist(
                    page_whatsapp,
                    request.deal_ids,
                )

            selected.extend(page_whatsapp)
            logger.info(
                "Deals source page %d: scanned %d rows, matched %d WhatsApp deals, selected %d",
                page_num,
                len(rows),
                len(page_whatsapp),
                len(selected),
            )

            if request.deal_ids and len(selected) >= len(request.deal_ids):
                return selected, raw_deals_scanned, whatsapp_matches

            if request.limit > 0 and len(selected) >= request.limit:
                return selected, raw_deals_scanned, whatsapp_matches

            next_start = response.get("next")
            if next_start is None:
                return selected, raw_deals_scanned, whatsapp_matches

            if page_delay > 0:
                time.sleep(page_delay)
            start = int(next_start)
            page_num += 1

    def _process_deal(
        self,
        deal: dict[str, Any],
        request: WhatsAppExportRequest,
        dirs: "_OutputDirectories",
        accumulator: _ExportAccumulator,
    ) -> None:
        deal_id = str(deal.get("ID", ""))
        paths = dirs.paths_for(deal_id)

        if request.skip_existing and paths.conversation.exists():
            accumulator.totals["skipped_existing"] += 1
            logger.info("Skipped deal ID=%s (conversation file already exists)", deal_id)
            return

        try:
            conversation = self._build_conversation_with_fallback(
                deal,
                deal_id,
                paths,
                include_system_messages=request.include_system_messages,
                date_from=request.date_from,
                date_to=request.date_to,
            )
            self._sink.write(paths.conversation, conversation.to_dict())
            accumulator.record_conversation(deal, conversation, paths.conversation)
            logger.info(
                "Exported WhatsApp history for deal ID=%s from %s binding: %d messages",
                deal_id,
                conversation.timeline_source,
                conversation.stats.total_messages,
            )
        except Exception as exc:  # noqa: BLE001 - per-deal isolation is intentional
            accumulator.errors.append({"deal_id": deal_id, "message": str(exc)})
            logger.warning("Failed to export deal ID=%s: %s", deal_id, exc)

    def _build_conversation_with_fallback(
        self,
        deal: dict[str, Any],
        deal_id: str,
        paths: "_DealPaths",
        *,
        include_system_messages: bool,
        date_from: str | None,
        date_to: str | None,
    ) -> WhatsAppConversation:
        deal_conversation = self._fetch_openline_conversation(
            deal=deal,
            entity_type="deal",
            entity_id=deal_id,
            raw_destination=paths.deal_openline_raw,
            include_system_messages=include_system_messages,
            date_from=date_from,
            date_to=date_to,
        )
        if deal_conversation is not None:
            return deal_conversation

        contact_id = str(deal.get("CONTACT_ID") or "")
        if not contact_id or contact_id == "0":
            return self._empty_conversation(
                deal,
                timeline_source="deal",
                timeline_entity_type="deal",
                timeline_entity_id=deal_id,
            )

        logger.info(
            "No Open Lines chat bound to deal ID=%s. Trying contact ID=%s",
            deal_id,
            contact_id,
        )
        contact_conversation = self._fetch_openline_conversation(
            deal=deal,
            entity_type="contact",
            entity_id=contact_id,
            raw_destination=paths.contact_openline_raw,
            include_system_messages=include_system_messages,
            date_from=date_from,
            date_to=date_to,
        )
        if contact_conversation is not None:
            logger.info(
                "Using contact Open Lines chat for deal ID=%s: %d messages",
                deal_id,
                contact_conversation.stats.total_messages,
            )
            return contact_conversation

        return self._empty_conversation(
            deal,
            timeline_source="deal",
            timeline_entity_type="deal",
            timeline_entity_id=deal_id,
        )

    def _fetch_openline_conversation(
        self,
        *,
        deal: dict[str, Any],
        entity_type: str,
        entity_id: str,
        raw_destination: Path,
        include_system_messages: bool,
        date_from: str | None,
        date_to: str | None,
    ) -> WhatsAppConversation | None:
        binding = self._find_chat_binding(entity_type=entity_type, entity_id=entity_id)
        raw_payload: dict[str, Any] = {
            "binding": None,
            "dialog": None,
            "history": None,
        }
        if binding is None:
            self._sink.write(raw_destination, raw_payload)
            return None

        raw_payload["binding"] = binding.to_dict()
        dialog = self._fetch_dialog(binding.chat_id)
        raw_payload["dialog"] = dialog

        session_id = self._extract_session_id(dialog)
        history = self._fetch_history(chat_id=binding.chat_id, session_id=session_id)
        raw_payload["history"] = history
        self._sink.write(raw_destination, raw_payload)

        return self._assemble_conversation(
            deal=deal,
            binding=binding,
            dialog=dialog,
            history=history,
            include_system_messages=include_system_messages,
            date_from=date_from,
            date_to=date_to,
        )

    def _find_chat_binding(
        self,
        *,
        entity_type: str,
        entity_id: str,
    ) -> _OpenLineBinding | None:
        response = self._gateway.call(
            "imopenlines.crm.chat.get",
            body={
                "CRM_ENTITY_TYPE": entity_type.upper(),
                "CRM_ENTITY": self._coerce_bitrix_id(entity_id),
                "ACTIVE_ONLY": "N",
            },
            label=f"imopenlines.crm.chat.get {entity_type} ID={entity_id}",
        )
        result = response.get("result") or []
        rows = result if isinstance(result, list) else [result]
        chats = [row for row in rows if isinstance(row, dict)]
        if not chats:
            return None

        whatsapp_chats = [chat for chat in chats if self._is_whatsapp_chat(chat)]
        selected = max(whatsapp_chats or chats, key=self._chat_sort_key)
        return _OpenLineBinding(
            timeline_source=entity_type,
            timeline_entity_type=entity_type,
            timeline_entity_id=entity_id,
            chat_id=str(selected.get("CHAT_ID") or ""),
            connector_id=str(selected.get("CONNECTOR_ID") or ""),
            connector_title=str(selected.get("CONNECTOR_TITLE") or ""),
        )

    def _fetch_dialog(self, chat_id: str) -> dict[str, Any]:
        response = self._gateway.call(
            "imopenlines.dialog.get",
            body={"CHAT_ID": self._coerce_bitrix_id(chat_id)},
            label=f"imopenlines.dialog.get chat ID={chat_id}",
        )
        result = response.get("result") or {}
        return result if isinstance(result, dict) else {}

    def _fetch_history(self, *, chat_id: str, session_id: str) -> dict[str, Any]:
        body = (
            {"SESSION_ID": self._coerce_bitrix_id(session_id)}
            if session_id
            else {"CHAT_ID": self._coerce_bitrix_id(chat_id)}
        )
        response = self._gateway.call(
            "imopenlines.session.history.get",
            body=body,
            label=f"imopenlines.session.history.get chat ID={chat_id}",
        )
        result = response.get("result") or {}
        return result if isinstance(result, dict) else {}

    def _assemble_conversation(
        self,
        *,
        deal: dict[str, Any],
        binding: _OpenLineBinding,
        dialog: dict[str, Any],
        history: dict[str, Any],
        include_system_messages: bool,
        date_from: str | None,
        date_to: str | None,
    ) -> WhatsAppConversation:
        users = history.get("users") or {}
        files_index = self._index_files(history.get("files"))
        messages = [
            self._parse_history_message(
                message=message,
                users=users,
                files_index=files_index,
                deal=deal,
            )
            for message in self._ordered_history_messages(history)
        ]
        messages = [
            message
            for message in messages
            if within_datetime_range(
                message.created_at,
                date_from=date_from,
                date_to=date_to,
            )
        ]
        if not include_system_messages:
            messages = [message for message in messages if not message.is_system_message]
        stats = ConversationStats.from_messages(messages)
        session_id = str(history.get("sessionId") or self._extract_session_id(dialog))
        return WhatsAppConversation(
            channel=self.CHANNEL,
            integration=self._integration_name(binding.connector_id, binding.connector_title),
            deal_id=str(deal.get("ID", "")),
            contact_id=str(deal.get("CONTACT_ID", "")),
            deal_title=str(deal.get("TITLE", "")),
            source_id=str(deal.get("SOURCE_ID", "")),
            assigned_by_id=str(deal.get("ASSIGNED_BY_ID", "")),
            stage_id=str(deal.get("STAGE_ID", "")),
            category_id=str(deal.get("CATEGORY_ID", "")),
            date_create=str(deal.get("DATE_CREATE", "")),
            date_modify=str(deal.get("DATE_MODIFY", "")),
            last_communication_time=str(deal.get("LAST_COMMUNICATION_TIME", "")),
            timeline_source=binding.timeline_source,
            timeline_entity_type=binding.timeline_entity_type,
            timeline_entity_id=binding.timeline_entity_id,
            chat_id=binding.chat_id,
            session_id=session_id,
            dialog_id=str(dialog.get("dialog_id") or dialog.get("dialogId") or ""),
            connector_id=binding.connector_id,
            connector_title=binding.connector_title,
            chat_name=str(dialog.get("name") or ""),
            stats=stats,
            messages=messages,
        )

    def _empty_conversation(
        self,
        deal: dict[str, Any],
        *,
        timeline_source: str,
        timeline_entity_type: str,
        timeline_entity_id: str,
    ) -> WhatsAppConversation:
        return WhatsAppConversation(
            channel=self.CHANNEL,
            integration="openlines",
            deal_id=str(deal.get("ID", "")),
            contact_id=str(deal.get("CONTACT_ID", "")),
            deal_title=str(deal.get("TITLE", "")),
            source_id=str(deal.get("SOURCE_ID", "")),
            assigned_by_id=str(deal.get("ASSIGNED_BY_ID", "")),
            stage_id=str(deal.get("STAGE_ID", "")),
            category_id=str(deal.get("CATEGORY_ID", "")),
            date_create=str(deal.get("DATE_CREATE", "")),
            date_modify=str(deal.get("DATE_MODIFY", "")),
            last_communication_time=str(deal.get("LAST_COMMUNICATION_TIME", "")),
            timeline_source=timeline_source,
            timeline_entity_type=timeline_entity_type,
            timeline_entity_id=timeline_entity_id,
            stats=ConversationStats.from_messages([]),
        )

    # ------------------------------------------------------------------
    # Open Lines message parsing
    # ------------------------------------------------------------------

    def _ordered_history_messages(self, history: dict[str, Any]) -> list[dict[str, Any]]:
        payload = history.get("message") or {}
        messages = payload.values() if isinstance(payload, dict) else payload
        rows = [row for row in messages if isinstance(row, dict)]
        return sorted(rows, key=self._history_message_sort_key)

    def _parse_history_message(
        self,
        *,
        message: dict[str, Any],
        users: dict[str, Any],
        files_index: dict[str, dict[str, Any]],
        deal: dict[str, Any],
    ) -> WhatsAppMessage:
        sender_id = str(message.get("senderid") or "")
        user = users.get(sender_id) if isinstance(users, dict) else None
        sender = user if isinstance(user, dict) else {}
        raw_text = str(message.get("text") or "")
        clean_text = self._clean_message_text(raw_text)
        direction, clean_text = self._extract_direction(clean_text)
        role = self._classify_sender(sender_id, sender, direction)
        return WhatsAppMessage(
            timeline_comment_id=str(message.get("id") or ""),
            message_id=str(message.get("id") or ""),
            created_at=str(message.get("date") or ""),
            author_id=sender_id,
            sender_role=role.value,
            sender_label=self._sender_label(sender, role, direction),
            text=clean_text,
            attachments=self._extract_attachments(message, files_index),
            is_system_message=(role is SenderRole.SYSTEM),
            raw_comment=raw_text,
            deal_id=str(deal.get("ID", "")),
        )

    def _classify_sender(
        self,
        sender_id: str,
        sender: dict[str, Any],
        direction: str,
    ) -> SenderRole:
        if not sender_id or sender_id == "0":
            return SenderRole.SYSTEM
        if direction == "outgoing":
            return SenderRole.MANAGER
        if direction == "incoming":
            return SenderRole.CLIENT
        if sender.get("connector") or str(sender.get("externalAuthId") or "").lower() == "imconnector":
            return SenderRole.CLIENT
        return SenderRole.MANAGER

    def _sender_label(
        self,
        sender: dict[str, Any],
        role: SenderRole,
        direction: str,
    ) -> str | None:
        if direction == "outgoing" and (
            sender.get("connector")
            or str(sender.get("externalAuthId") or "").lower() == "imconnector"
        ):
            return None
        raw_label = str(sender.get("name") or "")
        label = self._whitespace.normalize(raw_label)
        if not label or _PLACEHOLDER_LABEL.fullmatch(label):
            return None if role is not SenderRole.SYSTEM else "system"
        return label

    def _clean_message_text(self, text: str) -> str:
        decoded = html.unescape(text)
        stripped = self._bbcode_stripper.strip(decoded)
        stripped = _GENERIC_BBCODE_TAG.sub("", stripped)
        return self._whitespace.normalize(stripped)

    def _extract_direction(self, text: str) -> tuple[str, str]:
        if not text:
            return "unknown", text
        if _OUTGOING_MARKER.match(text):
            return "outgoing", self._whitespace.normalize(_OUTGOING_MARKER.sub("", text, count=1))
        if _INCOMING_MARKER.match(text):
            return "incoming", self._whitespace.normalize(_INCOMING_MARKER.sub("", text, count=1))
        return "unknown", text

    def _extract_attachments(
        self,
        message: dict[str, Any],
        files_index: dict[str, dict[str, Any]],
    ) -> list[WhatsAppAttachment]:
        params = message.get("params") or {}
        attachments: list[WhatsAppAttachment] = []

        for file_id in self._message_file_ids(params):
            file_info = files_index.get(file_id)
            if not file_info:
                continue
            attachment = self._attachment_from_file(file_info)
            if attachment is not None:
                attachments.append(attachment)

        attachments.extend(self._attachments_from_attach(params.get("attach")))
        return self._dedupe_attachments(attachments)

    def _message_file_ids(self, params: Any) -> list[str]:
        if not isinstance(params, dict):
            return []

        file_ids: list[str] = []
        for key in ("FILE_ID", "FILE_IDS", "fileId", "fileIds", "FILES", "files"):
            value = params.get(key)
            if value is None:
                continue
            if isinstance(value, list):
                file_ids.extend(str(item) for item in value if item is not None)
            else:
                file_ids.append(str(value))
        return file_ids

    def _attachment_from_file(self, file_info: dict[str, Any]) -> WhatsAppAttachment | None:
        url = ""
        for key in (
            "urldownload",
            "urlshow",
            "urlpreview",
            "urlDownload",
            "urlShow",
            "urlPreview",
            "url",
            "downloadUrl",
            "showUrl",
        ):
            candidate = file_info.get(key)
            if candidate:
                url = str(candidate)
                break

        if not url:
            return None

        label = str(
            file_info.get("name")
            or file_info.get("original_name")
            or file_info.get("fileName")
            or url
        )
        return WhatsAppAttachment(
            type=str(file_info.get("type") or "file"),
            label=self._whitespace.normalize(label),
            url=url,
        )

    def _attachments_from_attach(self, payload: Any) -> list[WhatsAppAttachment]:
        if not isinstance(payload, list):
            return []

        attachments: list[WhatsAppAttachment] = []
        for attach in payload:
            if not isinstance(attach, dict):
                continue
            blocks = attach.get("blocks") or []
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                for key in ("richLink", "link"):
                    entries = block.get(key) or []
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        url = str(entry.get("link") or entry.get("url") or "")
                        if not url:
                            continue
                        label = str(entry.get("name") or url)
                        attachments.append(
                            WhatsAppAttachment(
                                type="link",
                                label=self._whitespace.normalize(label),
                                url=url,
                            )
                        )
        return attachments

    def _dedupe_attachments(
        self,
        attachments: list[WhatsAppAttachment],
    ) -> list[WhatsAppAttachment]:
        seen: set[tuple[str, str, str]] = set()
        unique: list[WhatsAppAttachment] = []
        for attachment in attachments:
            key = (attachment.type, attachment.label, attachment.url)
            if key in seen:
                continue
            seen.add(key)
            unique.append(attachment)
        return unique

    def _index_files(self, payload: Any) -> dict[str, dict[str, Any]]:
        if isinstance(payload, dict):
            return {
                str(file_id): file_info
                for file_id, file_info in payload.items()
                if isinstance(file_info, dict)
            }
        if isinstance(payload, list):
            indexed: dict[str, dict[str, Any]] = {}
            for item in payload:
                if not isinstance(item, dict):
                    continue
                file_id = item.get("id") or item.get("ID") or item.get("fileId")
                if file_id is None:
                    continue
                indexed[str(file_id)] = item
            return indexed
        return {}

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def _write_reports(
        self,
        output_dir: Path,
        accumulator: _ExportAccumulator,
    ) -> None:
        report: dict[str, Any] = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "totals": accumulator.totals,
            "rows": accumulator.report_rows,
            "errors_count": len(accumulator.errors),
        }
        self._sink.write(output_dir / "report.json", report)
        self._sink.write(output_dir / "errors.json", accumulator.errors)

    def _log_summary(self, output_dir: Path, accumulator: _ExportAccumulator) -> None:
        totals = accumulator.totals
        logger.info("WhatsApp export completed.")
        logger.info("Deals scanned: %d", totals["deals_scanned"])
        logger.info("Chats exported: %d", totals["chats_exported"])
        logger.info("Messages exported: %d", totals["total_messages"])
        logger.info("Errors: %d", len(accumulator.errors))
        logger.info("Files saved to %s", output_dir.resolve())

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_whatsapp_chat(chat: dict[str, Any]) -> bool:
        connector_id = str(chat.get("CONNECTOR_ID") or "")
        connector_title = str(chat.get("CONNECTOR_TITLE") or "")
        return bool(_WHATSAPP_CONNECTOR.search(f"{connector_id} {connector_title}"))

    @staticmethod
    def _chat_sort_key(chat: dict[str, Any]) -> int:
        try:
            return int(chat.get("CHAT_ID") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _coerce_bitrix_id(value: str) -> int | str:
        return int(value) if str(value).isdigit() else value

    @staticmethod
    def _history_message_sort_key(message: dict[str, Any]) -> tuple[datetime, int]:
        raw_date = str(message.get("date") or "")
        try:
            parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.min.replace(tzinfo=timezone.utc)

        try:
            message_id = int(message.get("id") or 0)
        except (TypeError, ValueError):
            message_id = 0
        return parsed, message_id

    @staticmethod
    def _extract_session_id(dialog: dict[str, Any]) -> str:
        raw = str(dialog.get("entity_data_1") or dialog.get("entityData1") or "")
        parts = raw.split("|")
        if len(parts) > 5 and parts[5]:
            return parts[5]
        return ""

    @staticmethod
    def _integration_name(connector_id: str, connector_title: str) -> str:
        text = f"{connector_id} {connector_title}".lower()
        if "wazzup" in text:
            return "wazzup"
        if "whatsapp" in text:
            return "whatsapp"
        return "openlines"


# ---------------------------------------------------------------------------
# Path planning - kept private to this service
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DealPaths:
    conversation: Path
    deal_openline_raw: Path
    contact_openline_raw: Path


@dataclass(frozen=True)
class _OutputDirectories:
    root: Path
    raw: Path
    conversations: Path

    @classmethod
    def prepare(cls, root: Path) -> "_OutputDirectories":
        raw = root / "raw"
        conversations = root / "conversations"
        for path in (root, raw, conversations):
            path.mkdir(parents=True, exist_ok=True)
        return cls(root=root, raw=raw, conversations=conversations)

    def paths_for(self, deal_id: str) -> _DealPaths:
        return _DealPaths(
            conversation=self.conversations / f"deal_{deal_id}.json",
            deal_openline_raw=self.raw / f"deal_{deal_id}.openline.json",
            contact_openline_raw=self.raw / f"deal_{deal_id}.contact.openline.json",
        )
