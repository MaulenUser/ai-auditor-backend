from __future__ import annotations

from pathlib import Path

from ...domain.tenant import Tenant
from .connection import Database

_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS tenants (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_DDL_POSTGRES = """
CREATE TABLE IF NOT EXISTS tenants (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
)
"""

_SELECT_ONE = "SELECT id, name, created_at FROM tenants WHERE id = {p}"
_SELECT_ALL = "SELECT id, name, created_at FROM tenants ORDER BY created_at"


class TenantRepository:
    def __init__(self, db_path: Path) -> None:
        self._db = Database(db_path)
        with self._connect() as conn:
            conn.execute(_DDL_POSTGRES if self._db.is_postgres else _DDL_SQLITE)
        with self._connect() as conn:
            conn.execute(self._insert_ignore_sql(), ("default", "Default"))

    def _connect(self):
        return self._db.connect()

    def _insert_ignore_sql(self) -> str:
        p = self._db.placeholder
        return f"INSERT INTO tenants (id, name) VALUES ({p}, {p}) ON CONFLICT(id) DO NOTHING"

    def _upsert_sql(self) -> str:
        p = self._db.placeholder
        return (
            f"INSERT INTO tenants (id, name) VALUES ({p}, {p}) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name"
        )

    def get(self, tenant_id: str) -> Tenant | None:
        with self._connect() as conn:
            row = conn.execute(_SELECT_ONE.format(p=self._db.placeholder), (tenant_id,)).fetchone()
        if row is None:
            return None
        return Tenant(id=row["id"], name=row["name"], created_at=row["created_at"])

    def list_all(self) -> list[Tenant]:
        with self._connect() as conn:
            rows = conn.execute(_SELECT_ALL).fetchall()
        return [Tenant(id=r["id"], name=r["name"], created_at=r["created_at"]) for r in rows]

    def save(self, tenant: Tenant) -> None:
        with self._connect() as conn:
            conn.execute(self._upsert_sql(), (tenant.id, tenant.name))

    def ensure(self, tenant_id: str) -> Tenant:
        """Return existing tenant or create one with name equal to id."""
        with self._connect() as conn:
            conn.execute(self._insert_ignore_sql(), (tenant_id, tenant_id))
            row = conn.execute(_SELECT_ONE.format(p=self._db.placeholder), (tenant_id,)).fetchone()
        return Tenant(id=row["id"], name=row["name"], created_at=row["created_at"])
