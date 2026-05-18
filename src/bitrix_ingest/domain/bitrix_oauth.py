from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class BitrixOAuthToken:
    tenant_id: str
    bitrix_member_id: str
    bitrix_domain: str
    client_endpoint: str
    access_token: str
    refresh_token: str
    expires_at: int = 0
    scope: str = ""
    status: str = "active"
    created_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    def to_status_dict(self) -> dict:
        """Return connection status without exposing OAuth secrets."""
        return {
            "configured": bool(self.access_token and self.refresh_token),
            "status": self.status,
            "bitrix_member_id": self.bitrix_member_id,
            "bitrix_domain": self.bitrix_domain,
            "client_endpoint": self.client_endpoint,
            "scope": self.scope,
            "expires_at": self.expires_at,
        }
