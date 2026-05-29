from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...domain.analysis_run import AnalysisRun
from .connection import Database

_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS analysis_runs (
    run_id          TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    date_from       TEXT,
    date_to         TEXT,
    category_ids    TEXT NOT NULL DEFAULT '[]',
    responsible_ids TEXT NOT NULL DEFAULT '[]',
    output_dir      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT,
    error           TEXT,
    progress_stage      TEXT,
    progress_label      TEXT,
    progress_current    INTEGER NOT NULL DEFAULT 0,
    progress_total      INTEGER NOT NULL DEFAULT 0,
    progress_percent    REAL NOT NULL DEFAULT 0,
    progress_message    TEXT,
    eta_seconds         INTEGER,
    progress_updated_at TEXT
)
"""

_DDL_POSTGRES = """
CREATE TABLE IF NOT EXISTS analysis_runs (
    run_id          TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    date_from       TEXT,
    date_to         TEXT,
    category_ids    TEXT NOT NULL DEFAULT '[]',
    responsible_ids TEXT NOT NULL DEFAULT '[]',
    output_dir      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
    completed_at    TEXT,
    error           TEXT,
    progress_stage      TEXT,
    progress_label      TEXT,
    progress_current    INTEGER NOT NULL DEFAULT 0,
    progress_total      INTEGER NOT NULL DEFAULT 0,
    progress_percent    DOUBLE PRECISION NOT NULL DEFAULT 0,
    progress_message    TEXT,
    eta_seconds         INTEGER,
    progress_updated_at TEXT
)
"""

_PROGRESS_COLUMNS_SQLITE = {
    "progress_stage": "TEXT",
    "progress_label": "TEXT",
    "progress_current": "INTEGER NOT NULL DEFAULT 0",
    "progress_total": "INTEGER NOT NULL DEFAULT 0",
    "progress_percent": "REAL NOT NULL DEFAULT 0",
    "progress_message": "TEXT",
    "eta_seconds": "INTEGER",
    "progress_updated_at": "TEXT",
}

_PROGRESS_COLUMNS_POSTGRES = {
    "progress_stage": "TEXT",
    "progress_label": "TEXT",
    "progress_current": "INTEGER NOT NULL DEFAULT 0",
    "progress_total": "INTEGER NOT NULL DEFAULT 0",
    "progress_percent": "DOUBLE PRECISION NOT NULL DEFAULT 0",
    "progress_message": "TEXT",
    "eta_seconds": "INTEGER",
    "progress_updated_at": "TEXT",
}

_SELECT_ONE = "SELECT * FROM analysis_runs WHERE run_id = {p}"
_SELECT_BY_TENANT = (
    "SELECT * FROM analysis_runs WHERE tenant_id = {p} ORDER BY created_at DESC"
)


def _row_to_run(row: Any) -> AnalysisRun:
    return AnalysisRun(
        run_id=row["run_id"],
        tenant_id=row["tenant_id"],
        status=row["status"],
        date_from=row["date_from"],
        date_to=row["date_to"],
        category_ids=json.loads(row["category_ids"] or "[]"),
        responsible_ids=json.loads(row["responsible_ids"] or "[]"),
        output_dir=row["output_dir"] or "",
        created_at=row["created_at"],
        completed_at=row["completed_at"],
        error=row["error"],
        progress_stage=row["progress_stage"],
        progress_label=row["progress_label"],
        progress_current=int(row["progress_current"] or 0),
        progress_total=int(row["progress_total"] or 0),
        progress_percent=float(row["progress_percent"] or 0),
        progress_message=row["progress_message"],
        eta_seconds=row["eta_seconds"],
        progress_updated_at=row["progress_updated_at"],
    )


class AnalysisRunRepository:
    def __init__(self, db_path: Path) -> None:
        self._db = Database(db_path)
        with self._connect() as conn:
            conn.execute(_DDL_POSTGRES if self._db.is_postgres else _DDL_SQLITE)
            self._ensure_progress_columns(conn)

    def _connect(self):
        return self._db.connect()

    def _insert_sql(self) -> str:
        p = self._db.placeholder
        return f"""
INSERT INTO analysis_runs
    (run_id, tenant_id, status, date_from, date_to,
     category_ids, responsible_ids, output_dir)
VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
"""

    def _update_status_sql(self) -> str:
        p = self._db.placeholder
        return f"""
UPDATE analysis_runs
SET status = {p}, completed_at = {p}, error = {p}
WHERE run_id = {p}
"""

    def _update_progress_sql(self) -> str:
        p = self._db.placeholder
        return f"""
UPDATE analysis_runs
SET progress_stage = {p},
    progress_label = {p},
    progress_current = {p},
    progress_total = {p},
    progress_percent = {p},
    progress_message = {p},
    eta_seconds = {p},
    progress_updated_at = {p}
WHERE run_id = {p}
"""

    def _ensure_progress_columns(self, conn: Any) -> None:
        if self._db.is_postgres:
            for name, definition in _PROGRESS_COLUMNS_POSTGRES.items():
                conn.execute(
                    f"ALTER TABLE analysis_runs ADD COLUMN IF NOT EXISTS {name} {definition}"
                )
            return

        rows = conn.execute("PRAGMA table_info(analysis_runs)").fetchall()
        existing = {str(row["name"]) for row in rows}
        for name, definition in _PROGRESS_COLUMNS_SQLITE.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE analysis_runs ADD COLUMN {name} {definition}")

    def create(self, run: AnalysisRun) -> None:
        with self._connect() as conn:
            conn.execute(self._insert_sql(), (
                run.run_id,
                run.tenant_id,
                run.status,
                run.date_from,
                run.date_to,
                json.dumps(run.category_ids),
                json.dumps(run.responsible_ids),
                run.output_dir,
            ))

    def update_status(
        self,
        run_id: str,
        status: str,
        completed_at: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(self._update_status_sql(), (status, completed_at, error, run_id))

    def update_progress(
        self,
        run_id: str,
        *,
        stage: str | None,
        label: str | None,
        current: int,
        total: int,
        percent: float,
        message: str | None,
        eta_seconds: int | None,
        updated_at: str | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                self._update_progress_sql(),
                (
                    stage,
                    label,
                    current,
                    total,
                    percent,
                    message,
                    eta_seconds,
                    updated_at,
                    run_id,
                ),
            )

    def get(self, run_id: str) -> AnalysisRun | None:
        with self._connect() as conn:
            row = conn.execute(_SELECT_ONE.format(p=self._db.placeholder), (run_id,)).fetchone()
        return _row_to_run(row) if row else None

    def list_by_tenant(self, tenant_id: str) -> list[AnalysisRun]:
        with self._connect() as conn:
            rows = conn.execute(_SELECT_BY_TENANT.format(p=self._db.placeholder), (tenant_id,)).fetchall()
        return [_row_to_run(r) for r in rows]
