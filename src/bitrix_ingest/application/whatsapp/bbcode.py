"""BBCode parsing for WhatsApp comments.

Split into two objects with disjoint responsibilities:

- :class:`UrlAttachmentExtractor` reads file links out of the raw body.
- :class:`BBCodeStripper` rewrites the body for human display
  (strips ``[img]...[/img]`` and replaces ``[url=...]label[/url]`` with ``label``).
"""
from __future__ import annotations

import re

from ...domain.whatsapp import WhatsAppAttachment
from .text import WhitespaceNormalizer


_URL_ATTACHMENT = re.compile(r"\[url=(?P<url>[^\]]+)\](?P<label>.*?)\[/url\]", re.DOTALL)
_IMG_TAG = re.compile(r"\[img\][^\]]*?\[/img\]")
_URL_REPLACE = re.compile(r"\[url=[^\]]+\](?P<label>.*?)\[/url\]", re.DOTALL)


class UrlAttachmentExtractor:
    """Extracts ``[url=href]label[/url]`` pairs as :class:`WhatsAppAttachment`."""

    def __init__(self, whitespace: WhitespaceNormalizer | None = None) -> None:
        self._whitespace = whitespace or WhitespaceNormalizer()

    def extract(self, text: str) -> list[WhatsAppAttachment]:
        attachments: list[WhatsAppAttachment] = []
        for match in _URL_ATTACHMENT.finditer(text):
            attachments.append(
                WhatsAppAttachment(
                    type="file",
                    label=self._whitespace.normalize(match.group("label")),
                    url=match.group("url").strip(),
                )
            )
        return attachments


class BBCodeStripper:
    """Produces a human-readable form of a BBCode-laden comment.

    Strips ``[img]...[/img]`` tags entirely and collapses ``[url=...]label[/url]``
    to its label. Intended to run on text that has already been HTML-decoded.
    """

    def strip(self, text: str) -> str:
        text = _IMG_TAG.sub("", text)
        text = _URL_REPLACE.sub(lambda m: m.group("label"), text)
        return text
