"""ConversationAssembler — builds a :class:`WhatsAppConversation` from raw timeline comments."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ...domain.whatsapp import (
    ConversationStats,
    TimelineSource,
    WhatsAppConversation,
    WhatsAppMessage,
)
from .comment_parser import CommentParser


def _sort_messages(messages: list[WhatsAppMessage]) -> list[WhatsAppMessage]:
    def _key(m: WhatsAppMessage) -> tuple[datetime, str]:
        try:
            ts = datetime.fromisoformat(m.created_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            from datetime import timezone
            ts = datetime.min.replace(tzinfo=timezone.utc)
        return ts, m.timeline_comment_id

    return sorted(messages, key=_key)


def _normalize_last_comm_time(raw: str) -> str:
    """Convert Bitrix 'DD.MM.YYYY HH:MM:SS' to ISO 8601 when needed."""
    if not raw or "T" in raw:
        return raw
    try:
        dt = datetime.strptime(raw, "%d.%m.%Y %H:%M:%S")
        return dt.isoformat()
    except ValueError:
        return raw


class ConversationAssembler:
    """Applies the comment parser to every timeline entry and aggregates stats."""

    CHANNEL = "whatsapp"
    INTEGRATION = "wazzup"

    def __init__(self, parser: CommentParser | None = None) -> None:
        self._parser = parser or CommentParser()

    def assemble(
        self,
        deal: dict[str, Any],
        timeline_comments: list[dict[str, Any]],
        timeline_source: TimelineSource | str,
        timeline_entity_type: str,
        timeline_entity_id: str,
    ) -> WhatsAppConversation:
        messages = [
            message
            for comment in timeline_comments
            if (message := self._parser.parse(comment, deal)) is not None
        ]
        messages = _sort_messages(messages)
        stats = ConversationStats.from_messages(messages)
        source = timeline_source.value if isinstance(timeline_source, TimelineSource) else timeline_source

        return WhatsAppConversation(
            channel=self.CHANNEL,
            integration=self.INTEGRATION,
            deal_id=str(deal.get("ID", "")),
            contact_id=str(deal.get("CONTACT_ID", "")),
            deal_title=str(deal.get("TITLE", "")),
            source_id=str(deal.get("SOURCE_ID", "")),
            assigned_by_id=str(deal.get("ASSIGNED_BY_ID", "")),
            stage_id=str(deal.get("STAGE_ID", "")),
            stage_semantic_id=str(deal.get("STAGE_SEMANTIC_ID", "")),
            category_id=str(deal.get("CATEGORY_ID", "")),
            date_create=str(deal.get("DATE_CREATE", "")),
            date_modify=str(deal.get("DATE_MODIFY", "")),
            last_communication_time=_normalize_last_comm_time(
                str(deal.get("LAST_COMMUNICATION_TIME", ""))
            ),
            timeline_source=source,
            timeline_entity_type=timeline_entity_type,
            timeline_entity_id=timeline_entity_id,
            stats=stats,
            source="timeline" if stats.total_messages > 0 else "empty",
            messages=messages,
            opportunity=str(deal.get("OPPORTUNITY", "") or ""),
            currency_id=str(deal.get("CURRENCY_ID", "") or ""),
            closedate=str(deal.get("CLOSEDATE", "") or ""),
            closed=str(deal.get("CLOSED", "") or ""),
            loss_reason_id=str(deal.get("LOSS_REASON_ID", "") or ""),
            loss_comment=str(deal.get("LOSS_COMMENT", "") or ""),
            utm_source=str(deal.get("UTM_SOURCE", "") or ""),
            utm_medium=str(deal.get("UTM_MEDIUM", "") or ""),
            utm_campaign=str(deal.get("UTM_CAMPAIGN", "") or ""),
            company_id=str(deal.get("COMPANY_ID", "") or ""),
        )
