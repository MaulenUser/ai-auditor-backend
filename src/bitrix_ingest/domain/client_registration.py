from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ClientRegistration:
    email: str
    tenant_id: str
    phone: str
    name: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    def to_public_dict(self) -> dict:
        return {
            "email": self.email,
            "tenant_id": self.tenant_id,
            "phone": self.phone,
            "name": self.name,
            "created_at": self.created_at,
        }
