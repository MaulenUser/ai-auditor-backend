"""Structured JSON trace for long-running /audit/run executions."""
from __future__ import annotations

import json
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _duration_ms(started_at: datetime, finished_at: datetime) -> float:
    return round((finished_at - started_at).total_seconds() * 1000, 3)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _error_payload(exc: BaseException | None) -> dict[str, str] | None:
    if exc is None:
        return None
    return {
        "type": exc.__class__.__name__,
        "message": str(exc),
    }


@dataclass(frozen=True)
class _TraceEvent:
    sequence: int
    event_type: str
    name: str
    status: str
    started_at: datetime
    finished_at: datetime
    details: dict[str, Any] | None = None
    error: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sequence": self.sequence,
            "type": self.event_type,
            "name": self.name,
            "status": self.status,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "duration_ms": _duration_ms(self.started_at, self.finished_at),
        }
        if self.details:
            payload["details"] = _json_safe(self.details)
        if self.error:
            payload["error"] = _json_safe(self.error)
        return payload


class AuditTraceRecorder:
    """Writes a single JSON trace file that is updated during the run."""

    def __init__(
        self,
        trace_path: Path,
        *,
        run_name: str,
        request_details: dict[str, Any] | None = None,
    ) -> None:
        self._trace_path = trace_path
        self._trace_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._sequence = 0
        self._events: list[_TraceEvent] = []
        self._run_started_at = _utc_now()
        self._run_finished_at: datetime | None = None
        self._run_status = "running"
        self._run_name = run_name
        self._run_request = _json_safe(request_details or {})
        self._run_error: dict[str, str] | None = None
        self._flush()

    @property
    def trace_path(self) -> Path:
        return self._trace_path

    def span(
        self,
        event_type: str,
        name: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> "_TraceSpan":
        return _TraceSpan(self, event_type=event_type, name=name, details=details)

    def record_operation(
        self,
        event_type: str,
        name: str,
        *,
        started_at: datetime,
        finished_at: datetime,
        status: str,
        details: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        with self._lock:
            self._sequence += 1
            self._events.append(
                _TraceEvent(
                    sequence=self._sequence,
                    event_type=event_type,
                    name=name,
                    status=status,
                    started_at=started_at,
                    finished_at=finished_at,
                    details=_json_safe(details or {}),
                    error=_error_payload(error),
                )
            )
            self._flush_locked()

    def record_instant(
        self,
        event_type: str,
        name: str,
        *,
        status: str,
        details: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        now = _utc_now()
        self.record_operation(
            event_type,
            name,
            started_at=now,
            finished_at=now,
            status=status,
            details=details,
            error=error,
        )

    def finish(self, *, status: str, error: BaseException | None = None) -> None:
        with self._lock:
            self._run_status = status
            self._run_finished_at = _utc_now()
            self._run_error = _error_payload(error)
            self._flush_locked()

    def _snapshot(self) -> dict[str, Any]:
        finished_at = self._run_finished_at
        summary_counts: dict[str, int] = {}
        summary_durations: dict[str, float] = {}
        events = [event.to_dict() for event in self._events]
        for event in events:
            event_type = str(event["type"])
            summary_counts[event_type] = summary_counts.get(event_type, 0) + 1
            summary_durations[event_type] = round(
                summary_durations.get(event_type, 0.0) + float(event["duration_ms"]),
                3,
            )

        run: dict[str, Any] = {
            "name": self._run_name,
            "status": self._run_status,
            "started_at": _iso(self._run_started_at),
            "finished_at": _iso(finished_at),
            "duration_ms": (
                _duration_ms(self._run_started_at, finished_at)
                if finished_at is not None
                else None
            ),
            "request": self._run_request,
        }
        if self._run_error:
            run["error"] = self._run_error

        return {
            "trace_version": 1,
            "run": run,
            "summary": {
                "event_count": len(events),
                "counts_by_type": summary_counts,
                "durations_ms_by_type": summary_durations,
            },
            "events": events,
        }

    def _flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        payload = self._snapshot()
        tmp_path = self._trace_path.with_suffix(self._trace_path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._trace_path)


class _TraceSpan(AbstractContextManager["_TraceSpan"]):
    def __init__(
        self,
        recorder: AuditTraceRecorder,
        *,
        event_type: str,
        name: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._recorder = recorder
        self._event_type = event_type
        self._name = name
        self._details = details or {}
        self._started_at: datetime | None = None

    def __enter__(self) -> "_TraceSpan":
        self._started_at = _utc_now()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        started_at = self._started_at or _utc_now()
        self._recorder.record_operation(
            self._event_type,
            self._name,
            started_at=started_at,
            finished_at=_utc_now(),
            status="error" if exc is not None else "ok",
            details=self._details,
            error=exc,
        )
        return False


class TracedJsonSink:
    """Wraps any JsonSink and records write timings into the trace file."""

    def __init__(self, sink: Any, trace: AuditTraceRecorder) -> None:
        self._sink = sink
        self._trace = trace

    def write(self, path: str | Path, data: Any) -> None:
        destination = Path(path)
        started_at = _utc_now()
        try:
            self._sink.write(path, data)
        except Exception as exc:  # noqa: BLE001
            self._trace.record_operation(
                "json_write",
                "json.write",
                started_at=started_at,
                finished_at=_utc_now(),
                status="error",
                details={
                    "path": destination.as_posix(),
                    "data_type": type(data).__name__,
                },
                error=exc,
            )
            raise

        details: dict[str, Any] = {
            "path": destination.as_posix(),
            "data_type": type(data).__name__,
        }
        if isinstance(data, (list, dict)):
            details["item_count"] = len(data)
        if destination.exists():
            details["size_bytes"] = destination.stat().st_size

        self._trace.record_operation(
            "json_write",
            "json.write",
            started_at=started_at,
            finished_at=_utc_now(),
            status="ok",
            details=details,
        )
