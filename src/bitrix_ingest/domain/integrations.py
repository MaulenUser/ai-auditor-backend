from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class Integrations:
    bitrix_webhook_url: str = ""
    whatsapp_webhook_url: str = ""
    openai_api_key: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_status_dict(self) -> dict:
        """Return configuration status without exposing raw secrets."""
        return {
            "bitrix_webhook_url_configured": bool(self.bitrix_webhook_url),
            "whatsapp_webhook_url_configured": bool(self.whatsapp_webhook_url),
            "openai_api_key_configured": bool(self.openai_api_key),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Integrations:
        return cls(
            bitrix_webhook_url=d.get("bitrix_webhook_url") or "",
            whatsapp_webhook_url=d.get("whatsapp_webhook_url") or "",
            openai_api_key=d.get("openai_api_key") or "",
        )
