"""WhatsApp conversation domain entities."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SenderRole(str, Enum):
    """Who authored a WhatsApp message. Values match the PS output contract."""

    MANAGER = "manager"
    CLIENT = "client"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class TimelineSource(str, Enum):
    """Which Bitrix entity the timeline was pulled from."""

    DEAL = "deal"
    CONTACT = "contact"


@dataclass
class WhatsAppAttachment:
    """A file referenced inside a WhatsApp comment (as a `[url=...]label[/url]`)."""

    type: str
    label: str
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "label": self.label, "url": self.url}


@dataclass
class WhatsAppMessage:
    """A single normalised WhatsApp message extracted from a timeline comment."""

    timeline_comment_id: str
    created_at: str
    author_id: str
    sender_role: str
    sender_label: str | None
    text: str
    attachments: list[WhatsAppAttachment]
    is_system_message: bool
    raw_comment: str
    deal_id: str
    message_id: str = ""

    def has_attachments(self) -> bool:
        return bool(self.attachments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeline_comment_id": self.timeline_comment_id,
            "message_id": self.message_id,
            "created_at": self.created_at,
            "author_id": self.author_id,
            "sender_role": self.sender_role,
            "sender_label": self.sender_label,
            "text": self.text,
            "attachments": [a.to_dict() for a in self.attachments],
            "is_system_message": self.is_system_message,
            "raw_comment": self.raw_comment,
            "deal_id": self.deal_id,
        }


@dataclass
class ConversationStats:
    """Aggregate counts for a conversation. Derived from the message list."""

    total_messages: int
    manager_messages: int
    client_messages: int
    system_messages: int
    messages_with_files: int
    first_message_at: str | None
    last_message_at: str | None

    @classmethod
    def from_messages(cls, messages: list[WhatsAppMessage]) -> "ConversationStats":
        """Compute stats directly from a list of messages.

        Keeping this constructor on the stats object ensures there is exactly
        one place in the codebase that knows how to derive counts from messages.
        """
        if not messages:
            return cls(
                total_messages=0,
                manager_messages=0,
                client_messages=0,
                system_messages=0,
                messages_with_files=0,
                first_message_at=None,
                last_message_at=None,
            )

        role_counts = {role: 0 for role in SenderRole}
        files_count = 0
        for message in messages:
            try:
                role_counts[SenderRole(message.sender_role)] += 1
            except ValueError:
                role_counts[SenderRole.UNKNOWN] += 1
            if message.has_attachments():
                files_count += 1

        return cls(
            total_messages=len(messages),
            manager_messages=role_counts[SenderRole.MANAGER],
            client_messages=role_counts[SenderRole.CLIENT],
            system_messages=role_counts[SenderRole.SYSTEM],
            messages_with_files=files_count,
            first_message_at=messages[0].created_at,
            last_message_at=messages[-1].created_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_messages": self.total_messages,
            "manager_messages": self.manager_messages,
            "client_messages": self.client_messages,
            "system_messages": self.system_messages,
            "messages_with_files": self.messages_with_files,
            "first_message_at": self.first_message_at,
            "last_message_at": self.last_message_at,
        }


@dataclass
class WhatsAppConversation:
    """A WhatsApp conversation bound to a Bitrix CRM deal."""

    channel: str
    integration: str
    deal_id: str
    contact_id: str
    deal_title: str
    source_id: str
    assigned_by_id: str
    stage_id: str
    category_id: str
    date_create: str
    date_modify: str
    last_communication_time: str
    timeline_source: str
    timeline_entity_type: str
    timeline_entity_id: str
    stats: ConversationStats
    messages: list[WhatsAppMessage] = field(default_factory=list)
    chat_id: str = ""
    session_id: str = ""
    dialog_id: str = ""
    connector_id: str = ""
    connector_title: str = ""
    chat_name: str = ""

    @property
    def is_empty(self) -> bool:
        return self.stats.total_messages == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "integration": self.integration,
            "deal_id": self.deal_id,
            "contact_id": self.contact_id,
            "deal_title": self.deal_title,
            "source_id": self.source_id,
            "assigned_by_id": self.assigned_by_id,
            "stage_id": self.stage_id,
            "category_id": self.category_id,
            "date_create": self.date_create,
            "date_modify": self.date_modify,
            "last_communication_time": self.last_communication_time,
            "timeline_source": self.timeline_source,
            "timeline_entity_type": self.timeline_entity_type,
            "timeline_entity_id": self.timeline_entity_id,
            "chat_id": self.chat_id,
            "session_id": self.session_id,
            "dialog_id": self.dialog_id,
            "connector_id": self.connector_id,
            "connector_title": self.connector_title,
            "chat_name": self.chat_name,
            "stats": self.stats.to_dict(),
            "messages": [m.to_dict() for m in self.messages],
        }
