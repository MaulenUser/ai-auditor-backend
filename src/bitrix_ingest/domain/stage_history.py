"""Stage history domain entities."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _to_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class StageInterval:
    """One stage a deal spent time in."""

    stage_id: str
    stage_name: str
    entered_at: str
    left_at: str | None
    duration_hours: float | None
    moved_by_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "stage_name": self.stage_name,
            "entered_at": self.entered_at,
            "left_at": self.left_at,
            "duration_hours": self.duration_hours,
            "moved_by_id": self.moved_by_id,
        }


@dataclass
class DealStageHistory:
    """Full stage timeline for one deal."""

    deal_id: str
    stages: list[StageInterval]

    @property
    def total_stages_visited(self) -> int:
        return len(self.stages)

    @property
    def current_stage_id(self) -> str | None:
        return self.stages[-1].stage_id if self.stages else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "deal_id": self.deal_id,
            "total_stages_visited": self.total_stages_visited,
            "current_stage_id": self.current_stage_id,
            "stages": [s.to_dict() for s in self.stages],
        }

    @classmethod
    def from_raw(
        cls,
        deal_id: str,
        rows: list[dict[str, Any]],
        stage_names: dict[str, str],
    ) -> "DealStageHistory":
        """Build from a sorted crm.stagehistory.list response."""
        # Bitrix field names can vary; handle both conventions.
        def _stage_id(row: dict[str, Any]) -> str:
            return str(row.get("STAGE_ID") or row.get("stage_id") or "")

        def _move_time(row: dict[str, Any]) -> str:
            return str(
                row.get("CREATED_TIME")
                or row.get("createdTime")
                or row.get("MOVE_TIME")
                or row.get("moveTime")
                or ""
            )

        def _moved_by(row: dict[str, Any]) -> str:
            return str(
                row.get("MOVED_BY_ID")
                or row.get("movedById")
                or row.get("CREATED_BY_ID")
                or ""
            )

        intervals: list[StageInterval] = []
        for i, row in enumerate(rows):
            entered_at = _move_time(row)
            next_row = rows[i + 1] if i + 1 < len(rows) else None
            left_at = _move_time(next_row) if next_row else None

            duration_hours: float | None = None
            if entered_at and left_at:
                t1 = _parse_ts(entered_at)
                t2 = _parse_ts(left_at)
                if t1 and t2:
                    duration_hours = (_to_utc(t2) - _to_utc(t1)).total_seconds() / 3600.0

            sid = _stage_id(row)
            intervals.append(StageInterval(
                stage_id=sid,
                stage_name=stage_names.get(sid, sid),
                entered_at=entered_at,
                left_at=left_at,
                duration_hours=duration_hours,
                moved_by_id=_moved_by(row),
            ))

        return cls(deal_id=deal_id, stages=intervals)
