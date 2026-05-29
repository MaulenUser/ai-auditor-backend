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
    progress_stage: str | None = None
    progress_label: str | None = None
    progress_current: int = 0
    progress_total: int = 0
    progress_percent: float = 0.0
    progress_message: str | None = None
    eta_seconds: int | None = None
    progress_updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        progress = {
            "stage": self.progress_stage,
            "label": self.progress_label,
            "current": self.progress_current,
            "total": self.progress_total,
            "percent": self.progress_percent,
            "message": self.progress_message,
            "eta_seconds": self.eta_seconds,
            "updated_at": self.progress_updated_at,
        }
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
            "progress_stage": self.progress_stage,
            "progress_label": self.progress_label,
            "progress_current": self.progress_current,
            "progress_total": self.progress_total,
            "progress_percent": self.progress_percent,
            "progress_message": self.progress_message,
            "eta_seconds": self.eta_seconds,
            "progress_updated_at": self.progress_updated_at,
            "progress": progress,
        }
