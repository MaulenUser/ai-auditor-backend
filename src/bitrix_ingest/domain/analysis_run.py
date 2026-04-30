from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class AnalysisRun:
    run_id: str
    tenant_id: str
    status: str = "queued"
    date_from: str | None = None
    date_to: str | None = None
    category_ids: list[str] = field(default_factory=list)
    responsible_ids: list[str] = field(default_factory=list)
    output_dir: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )
    completed_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "status": self.status,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "category_ids": self.category_ids,
            "responsible_ids": self.responsible_ids,
            "output_dir": self.output_dir,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }
