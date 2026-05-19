from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class BitrixConnectSession:
    connection_code: str
    tenant_id: str
    bitrix_domain: str
    return_url: str = ""
    expires_at: int = 0
    status: str = "pending"
    used_at: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    def to_public_dict(self) -> dict:
        return {
            "connection_code": self.connection_code,
            "tenant_id": self.tenant_id,
            "bitrix_domain": self.bitrix_domain,
            "return_url": self.return_url,
            "expires_at": self.expires_at,
            "status": self.status,
            "used_at": self.used_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
