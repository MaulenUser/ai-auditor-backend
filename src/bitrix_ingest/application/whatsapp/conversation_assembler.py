"""ConversationAssembler — builds a :class:`WhatsAppConversation` from raw timeline comments."""
from __future__ import annotations

from typing import Any

from ...domain.whatsapp import (
    ConversationStats,
    TimelineSource,
    WhatsAppConversation,
)
from .comment_parser import CommentParser


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
            category_id=str(deal.get("CATEGORY_ID", "")),
            date_create=str(deal.get("DATE_CREATE", "")),
            date_modify=str(deal.get("DATE_MODIFY", "")),
            last_communication_time=str(deal.get("LAST_COMMUNICATION_TIME", "")),
            timeline_source=source,
            timeline_entity_type=timeline_entity_type,
            timeline_entity_id=timeline_entity_id,
            stats=stats,
            messages=messages,
        )
