"""WhatsApp conversation domain entities."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    source: str = ""

    def has_attachments(self) -> bool:
        return bool(self.attachments)

    def to_dict(self) -> dict[str, Any]:
        # raw_comment is preserved in raw/*.json; omitted here to reduce file size.
        # message_id duplicates timeline_comment_id and is intentionally omitted.
        return {
            "timeline_comment_id": self.timeline_comment_id,
            "created_at": self.created_at,
            "author_id": self.author_id,
            "sender_role": self.sender_role,
            "sender_label": self.sender_label,
            "text": self.text,
            "attachments": [a.to_dict() for a in self.attachments],
            "is_system_message": self.is_system_message,
            "deal_id": self.deal_id,
            "source": self.source,
        }


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class ConversationStats:
    """Aggregate counts and derived KPIs for a conversation."""

    total_messages: int
    manager_messages: int
    client_messages: int
    system_messages: int
    messages_with_files: int
    first_message_at: str | None
    last_message_at: str | None
    # Derived analytics fields
    effective_messages: int = 0
    first_manager_response_time_sec: float | None = None
    avg_response_latency_sec: float | None = None
    conversation_duration_hours: float | None = None

    @classmethod
    def from_messages(cls, messages: list[WhatsAppMessage]) -> "ConversationStats":
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

        effective = role_counts[SenderRole.MANAGER] + role_counts[SenderRole.CLIENT]
        first_response = _compute_first_response_time(messages)
        avg_latency = _compute_avg_response_latency(messages)
        duration = _compute_duration_hours(messages)

        return cls(
            total_messages=len(messages),
            manager_messages=role_counts[SenderRole.MANAGER],
            client_messages=role_counts[SenderRole.CLIENT],
            system_messages=role_counts[SenderRole.SYSTEM],
            messages_with_files=files_count,
            first_message_at=messages[0].created_at,
            last_message_at=messages[-1].created_at,
            effective_messages=effective,
            first_manager_response_time_sec=first_response,
            avg_response_latency_sec=avg_latency,
            conversation_duration_hours=duration,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_messages": self.total_messages,
            "effective_messages": self.effective_messages,
            "manager_messages": self.manager_messages,
            "client_messages": self.client_messages,
            "system_messages": self.system_messages,
            "messages_with_files": self.messages_with_files,
            "first_message_at": self.first_message_at,
            "last_message_at": self.last_message_at,
            "first_manager_response_time_sec": self.first_manager_response_time_sec,
            "avg_response_latency_sec": self.avg_response_latency_sec,
            "conversation_duration_hours": self.conversation_duration_hours,
        }


def _compute_first_response_time(messages: list[WhatsAppMessage]) -> float | None:
    """Seconds from first client message to first subsequent manager message."""
    first_client_ts: datetime | None = None
    for msg in messages:
        if msg.sender_role == SenderRole.CLIENT:
            first_client_ts = _parse_ts(msg.created_at)
            break
    if first_client_ts is None:
        return None
    first_client_ts = _to_utc(first_client_ts)
    for msg in messages:
        if msg.sender_role != SenderRole.MANAGER:
            continue
        ts = _parse_ts(msg.created_at)
        if ts is None:
            continue
        ts = _to_utc(ts)
        if ts >= first_client_ts:
            return (ts - first_client_ts).total_seconds()
    return None


def _compute_avg_response_latency(messages: list[WhatsAppMessage]) -> float | None:
    """Mean seconds from each client message to the next manager message."""
    latencies: list[float] = []
    pending_client_ts: datetime | None = None
    for msg in messages:
        if msg.sender_role == SenderRole.CLIENT:
            ts = _parse_ts(msg.created_at)
            if ts is not None:
                pending_client_ts = _to_utc(ts)
        elif msg.sender_role == SenderRole.MANAGER and pending_client_ts is not None:
            ts = _parse_ts(msg.created_at)
            if ts is not None:
                ts = _to_utc(ts)
                if ts >= pending_client_ts:
                    latencies.append((ts - pending_client_ts).total_seconds())
            pending_client_ts = None
    if not latencies:
        return None
    return sum(latencies) / len(latencies)


def _compute_duration_hours(messages: list[WhatsAppMessage]) -> float | None:
    if len(messages) < 2:
        return None
    first = _parse_ts(messages[0].created_at)
    last = _parse_ts(messages[-1].created_at)
    if first is None or last is None:
        return None
    return (_to_utc(last) - _to_utc(first)).total_seconds() / 3600.0


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
    stage_semantic_id: str
    category_id: str
    date_create: str
    date_modify: str
    last_communication_time: str
    timeline_source: str
    timeline_entity_type: str
    timeline_entity_id: str
    stats: ConversationStats
    source: str = ""
    messages: list[WhatsAppMessage] = field(default_factory=list)
    chat_id: str = ""
    session_id: str = ""
    dialog_id: str = ""
    connector_id: str = ""
    connector_title: str = ""
    chat_name: str = ""
    # Deal analytics fields
    opportunity: str = ""
    currency_id: str = ""
    closedate: str = ""
    closed: str = ""
    loss_reason_id: str = ""
    loss_comment: str = ""
    utm_source: str = ""
    utm_medium: str = ""
    utm_campaign: str = ""
    company_id: str = ""

    @property
    def is_empty(self) -> bool:
        return self.stats.total_messages == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "integration": self.integration,
            "deal_id": self.deal_id,
            "contact_id": self.contact_id,
            "company_id": self.company_id,
            "deal_title": self.deal_title,
            "source_id": self.source_id,
            "assigned_by_id": self.assigned_by_id,
            "stage_id": self.stage_id,
            "stage_semantic_id": self.stage_semantic_id,
            "category_id": self.category_id,
            "date_create": self.date_create,
            "date_modify": self.date_modify,
            "closedate": self.closedate,
            "closed": self.closed,
            "last_communication_time": self.last_communication_time,
            "opportunity": self.opportunity,
            "currency_id": self.currency_id,
            "loss_reason_id": self.loss_reason_id,
            "loss_comment": self.loss_comment,
            "utm_source": self.utm_source,
            "utm_medium": self.utm_medium,
            "utm_campaign": self.utm_campaign,
            "timeline_source": self.timeline_source,
            "timeline_entity_type": self.timeline_entity_type,
            "timeline_entity_id": self.timeline_entity_id,
            "source": self.source,
            "chat_id": self.chat_id,
            "session_id": self.session_id,
            "dialog_id": self.dialog_id,
            "connector_id": self.connector_id,
            "connector_title": self.connector_title,
            "chat_name": self.chat_name,
            "stats": self.stats.to_dict(),
            "messages": [m.to_dict() for m in self.messages],
        }
