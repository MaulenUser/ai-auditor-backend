from __future__ import annotations

from pathlib import Path
from typing import Any

from ...domain.client_registration import ClientRegistration
from .connection import Database

_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS client_registrations (
    email      TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL UNIQUE,
    phone      TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_DDL_POSTGRES = """
CREATE TABLE IF NOT EXISTS client_registrations (
    email      TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL UNIQUE,
    phone      TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
    updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
)
"""

_INDEX_TENANT = """
CREATE INDEX IF NOT EXISTS idx_client_registrations_tenant_id
ON client_registrations(tenant_id)
"""

_SELECT_COLUMNS = "email, tenant_id, phone, name, created_at"

_SELECT_BY_EMAIL = f"""
SELECT {_SELECT_COLUMNS}
FROM client_registrations
WHERE email = {{p}}
"""

_SELECT_BY_TENANT = f"""
SELECT {_SELECT_COLUMNS}
FROM client_registrations
WHERE tenant_id = {{p}}
"""


class ClientRegistrationRepository:
    def __init__(self, db_path: Path) -> None:
        self._db = Database(db_path)
        with self._connect() as conn:
            conn.execute(_DDL_POSTGRES if self._db.is_postgres else _DDL_SQLITE)
            conn.execute(_INDEX_TENANT)

    def _connect(self):
        return self._db.connect()

    def _upsert_sql(self) -> str:
        p = self._db.placeholder
        return f"""
INSERT INTO client_registrations
    (email, tenant_id, phone, name, updated_at)
VALUES ({p}, {p}, {p}, {p}, {self._db.now_sql})
ON CONFLICT(email) DO UPDATE SET
    tenant_id  = excluded.tenant_id,
    phone      = excluded.phone,
    name       = excluded.name,
    updated_at = {self._db.now_sql}
"""

    def save(self, registration: ClientRegistration) -> None:
        with self._connect() as conn:
            conn.execute(
                self._upsert_sql(),
                (
                    registration.email,
                    registration.tenant_id,
                    registration.phone,
                    registration.name,
                ),
            )

    def get_by_email(self, email: str) -> ClientRegistration | None:
        with self._connect() as conn:
            row = conn.execute(
                _SELECT_BY_EMAIL.format(p=self._db.placeholder),
                (email,),
            ).fetchone()
        return self._row_to_registration(row) if row is not None else None

    def get_by_tenant(self, tenant_id: str) -> ClientRegistration | None:
        with self._connect() as conn:
            row = conn.execute(
                _SELECT_BY_TENANT.format(p=self._db.placeholder),
                (tenant_id,),
            ).fetchone()
        return self._row_to_registration(row) if row is not None else None

    @staticmethod
    def _row_to_registration(row: Any) -> ClientRegistration:
        return ClientRegistration(
            email=row["email"],
            tenant_id=row["tenant_id"],
            phone=row["phone"],
            name=row["name"],
            created_at=row["created_at"],
        )
