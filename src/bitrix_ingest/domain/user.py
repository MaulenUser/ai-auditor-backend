from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class User:
    username: str
    tenant_id: str
    role: str = "client"
    password_hash: str = ""
    active: bool = True
    created_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    def to_public_dict(self) -> dict:
        return {
            "username": self.username,
            "tenant_id": self.tenant_id,
            "role": self.role,
            "active": self.active,
            "created_at": self.created_at,
        }

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"
