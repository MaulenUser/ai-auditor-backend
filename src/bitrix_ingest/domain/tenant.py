from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Tenant:
    id: str
    name: str
    created_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d: dict) -> "Tenant":
        return cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            created_at=d.get("created_at", ""),
        )
