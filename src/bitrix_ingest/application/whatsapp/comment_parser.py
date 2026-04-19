"""CommentParser — converts a raw timeline comment into a WhatsAppMessage.

Composes:
  detection  → is this a WhatsApp comment?
  attachment extractor → collect file links (pre-HTML-decode, on the raw body)
  BBCode stripper      → clean text for display (post-HTML-decode)
  whitespace normaliser → final cleanup
  sender classifier    → role + sender label
"""
from __future__ import annotations

import html
from typing import Any

from ...domain.whatsapp import SenderRole, WhatsAppMessage
from .bbcode import BBCodeStripper, UrlAttachmentExtractor
from .detection import WhatsAppMarkerDetector
from .sender import SenderRoleClassifier
from .text import WhitespaceNormalizer


class CommentParser:
    """Orchestrates comment → :class:`WhatsAppMessage` translation."""

    def __init__(
        self,
        detector: WhatsAppMarkerDetector | None = None,
        attachment_extractor: UrlAttachmentExtractor | None = None,
        bbcode_stripper: BBCodeStripper | None = None,
        whitespace: WhitespaceNormalizer | None = None,
        sender_classifier: SenderRoleClassifier | None = None,
    ) -> None:
        self._whitespace = whitespace or WhitespaceNormalizer()
        self._detector = detector or WhatsAppMarkerDetector()
        self._attachment_extractor = (
            attachment_extractor or UrlAttachmentExtractor(self._whitespace)
        )
        self._bbcode_stripper = bbcode_stripper or BBCodeStripper()
        self._sender_classifier = sender_classifier or SenderRoleClassifier()

    def parse(self, comment: dict[str, Any], deal: dict[str, Any]) -> WhatsAppMessage | None:
        """Return a :class:`WhatsAppMessage` if the comment is a WhatsApp one, else ``None``."""
        raw_comment = str(comment.get("COMMENT") or "")

        if not self._detector.is_whatsapp(raw_comment):
            return None

        attachments = self._attachment_extractor.extract(raw_comment)
        clean_text = self._prepare_text(raw_comment)
        sender_info = self._sender_classifier.extract(clean_text)

        role = self._sender_classifier.classify(
            sender_label=sender_info.label,
            message_text=sender_info.message,
            deal=deal,
        )

        return WhatsAppMessage(
            timeline_comment_id=str(comment.get("ID", "")),
            created_at=str(comment.get("CREATED", "")),
            author_id=str(comment.get("AUTHOR_ID", "")),
            sender_role=role.value,
            sender_label=sender_info.label,
            text=sender_info.message,
            attachments=attachments,
            is_system_message=(role is SenderRole.SYSTEM),
            raw_comment=raw_comment,
            deal_id=str(deal.get("ID", "")),
        )

    def _prepare_text(self, raw_comment: str) -> str:
        decoded = html.unescape(raw_comment)
        stripped = self._bbcode_stripper.strip(decoded)
        return self._whitespace.normalize(stripped)
