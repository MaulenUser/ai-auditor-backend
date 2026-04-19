"""Value object for a Bitrix24 webhook URL."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WebhookUrl:
    """Normalised Bitrix24 webhook URL, guaranteed to end with ``/``.

    Storing the URL as a value object means callers can pass it around with
    the invariant preserved, without each layer having to re-normalise the
    raw string.
    """

    value: str

    @classmethod
    def parse(cls, raw: str) -> "WebhookUrl":
        stripped = raw.strip()
        normalised = stripped if stripped.endswith("/") else f"{stripped}/"
        return cls(value=normalised)

    def method_url(self, method: str) -> str:
        """Build the full URL for a Bitrix API *method* (e.g. ``crm.deal.list``)."""
        return f"{self.value}{method}.json"

    def __str__(self) -> str:
        return self.value
