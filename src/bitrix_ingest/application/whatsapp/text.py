"""Whitespace normalisation for WhatsApp comments."""
from __future__ import annotations

import re


class WhitespaceNormalizer:
    """Normalises NBSP → space, CRLF → LF, collapses runs of whitespace per line.

    The per-line pass matters: callers need to preserve line breaks because
    the sender label is detected via the ``Name:\\n`` prefix pattern.
    """

    _RUN_OF_WHITESPACE = re.compile(r"\s+")

    def normalize(self, text: str) -> str:
        text = text.replace("\u00a0", " ")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [self._RUN_OF_WHITESPACE.sub(" ", line).strip() for line in text.split("\n")]
        return "\n".join(lines).strip()

    def __call__(self, text: str) -> str:  # ergonomic alias
        return self.normalize(text)
