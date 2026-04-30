from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any


def _database_url() -> str:
    return (
        os.environ.get("BITRIX_DATABASE_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
    )


class Database:
    """Small SQLite/PostgreSQL adapter for the repository layer."""

    def __init__(self, sqlite_path: Path) -> None:
        self.sqlite_path = sqlite_path
        self.url = _database_url()

    @property
    def is_postgres(self) -> bool:
        return self.url.startswith(("postgresql://", "postgres://"))

    @property
    def placeholder(self) -> str:
        return "%s" if self.is_postgres else "?"

    @property
    def now_sql(self) -> str:
        return "(CURRENT_TIMESTAMP::text)" if self.is_postgres else "(datetime('now'))"

    def connect(self) -> Any:
        if self.is_postgres:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL is configured but psycopg is not installed. "
                    "Install with: pip install -e \".[api,postgres]\""
                ) from exc
            url = self.url.replace("postgres://", "postgresql://", 1)
            return psycopg.connect(url, row_factory=dict_row)

        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn
