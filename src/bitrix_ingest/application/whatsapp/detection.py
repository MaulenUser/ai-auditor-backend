"""WhatsApp-marker detection.

Checks whether a raw timeline comment is part of the Wazzup24 WhatsApp
integration by scanning for any of the markers the integration embeds
(``wazzup24``, ``whatsapp.png``, ``SYSTEM WZ``).
"""
from __future__ import annotations

import re


_DEFAULT_PATTERN = re.compile(r"wazzup24|whatsapp\.png|SYSTEM WZ", re.IGNORECASE)


class WhatsAppMarkerDetector:
    """Answers: 'does this comment look like a WhatsApp message?'."""

    def __init__(self, pattern: re.Pattern[str] = _DEFAULT_PATTERN) -> None:
        self._pattern = pattern

    def is_whatsapp(self, comment_text: str) -> bool:
        return bool(self._pattern.search(comment_text))
