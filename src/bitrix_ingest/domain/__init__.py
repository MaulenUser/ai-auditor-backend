"""Domain layer — pure entities and business rules. No I/O, no framework deps."""
from .call_records import CallScanError, CallScanRow
from .exceptions import BitrixError, DomainError
from .whatsapp import (
    ConversationStats,
    SenderRole,
    TimelineSource,
    WhatsAppAttachment,
    WhatsAppConversation,
    WhatsAppMessage,
)

__all__ = [
    "BitrixError",
    "CallScanError",
    "CallScanRow",
    "ConversationStats",
    "DomainError",
    "SenderRole",
    "TimelineSource",
    "WhatsAppAttachment",
    "WhatsAppConversation",
    "WhatsAppMessage",
]
