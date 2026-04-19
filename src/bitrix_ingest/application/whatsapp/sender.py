"""Sender-role classification for normalised WhatsApp comments."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...domain.whatsapp import SenderRole


_SYSTEM_MARKER = "=== SYSTEM WZ ==="


@dataclass(frozen=True)
class SenderInfo:
    """The extracted sender label and the body with that line removed."""

    label: str | None
    message: str


class SenderRoleClassifier:
    """Extracts the sender label and decides whether they're client/manager/system."""

    def extract(self, clean_text: str) -> SenderInfo:
        """Look for a ``Name:`` prefix line; return label and remaining body."""
        newline_idx = clean_text.find("\n")
        if newline_idx < 0:
            return SenderInfo(label=None, message=clean_text)

        first_line = clean_text[:newline_idx].strip()
        remaining = clean_text[newline_idx + 1:].strip()
        if not first_line.endswith(":"):
            return SenderInfo(label=None, message=clean_text)
        return SenderInfo(label=first_line.rstrip(":").strip(), message=remaining)

    def classify(self, *, sender_label: str | None, message_text: str, deal: dict[str, Any]) -> SenderRole:
        if message_text.startswith(_SYSTEM_MARKER):
            return SenderRole.SYSTEM
        if sender_label is None:
            return SenderRole.UNKNOWN
        if sender_label == str(deal.get("TITLE", "")):
            return SenderRole.CLIENT
        return SenderRole.MANAGER
