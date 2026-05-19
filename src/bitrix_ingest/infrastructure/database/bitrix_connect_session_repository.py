from __future__ import annotations

from pathlib import Path
from typing import Any

from ...domain.bitrix_connect_session import BitrixConnectSession
from .connection import Database

_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS bitrix_connect_sessions (
    connection_code TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    bitrix_domain   TEXT NOT NULL DEFAULT '',
    return_url      TEXT NOT NULL DEFAULT '',
    expires_at      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',
    used_at         TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_DDL_POSTGRES = """
CREATE TABLE IF NOT EXISTS bitrix_connect_sessions (
    connection_code TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    bitrix_domain   TEXT NOT NULL DEFAULT '',
    return_url      TEXT NOT NULL DEFAULT '',
    expires_at      BIGINT NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',
    used_at         TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
    updated_at      TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
)
"""

_INDEX_TENANT = """
CREATE INDEX IF NOT EXISTS idx_bitrix_connect_sessions_tenant_id
ON bitrix_connect_sessions(tenant_id)
"""

_INDEX_DOMAIN = """
CREATE INDEX IF NOT EXISTS idx_bitrix_connect_sessions_domain
ON bitrix_connect_sessions(bitrix_domain)
"""

_SELECT_COLUMNS = """
connection_code, tenant_id, bitrix_domain, return_url, expires_at,
status, used_at, created_at, updated_at
"""

_SELECT_BY_CODE = f"""
SELECT {_SELECT_COLUMNS}
FROM bitrix_connect_sessions
WHERE connection_code = {{p}}
"""


class BitrixConnectSessionRepository:
    def __init__(self, db_path: Path) -> None:
        self._db = Database(db_path)
        with self._connect() as conn:
            conn.execute(_DDL_POSTGRES if self._db.is_postgres else _DDL_SQLITE)
            conn.execute(_INDEX_TENANT)
            conn.execute(_INDEX_DOMAIN)

    def _connect(self):
        return self._db.connect()

    def _upsert_sql(self) -> str:
        p = self._db.placeholder
        return f"""
INSERT INTO bitrix_connect_sessions
    (connection_code, tenant_id, bitrix_domain, return_url,
     expires_at, status, updated_at)
VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {self._db.now_sql})
ON CONFLICT(connection_code) DO UPDATE SET
    tenant_id     = excluded.tenant_id,
    bitrix_domain = excluded.bitrix_domain,
    return_url    = excluded.return_url,
    expires_at    = excluded.expires_at,
    status        = excluded.status,
    updated_at    = {self._db.now_sql}
"""

    def _mark_used_sql(self) -> str:
        p = self._db.placeholder
        return f"""
UPDATE bitrix_connect_sessions
SET status = 'used', used_at = {self._db.now_sql}, updated_at = {self._db.now_sql}
WHERE connection_code = {p}
"""

    def save(self, session: BitrixConnectSession) -> None:
        with self._connect() as conn:
            conn.execute(
                self._upsert_sql(),
                (
                    session.connection_code,
                    session.tenant_id,
                    session.bitrix_domain,
                    session.return_url,
                    int(session.expires_at),
                    session.status,
                ),
            )

    def get_by_code(self, connection_code: str) -> BitrixConnectSession | None:
        with self._connect() as conn:
            row = conn.execute(
                _SELECT_BY_CODE.format(p=self._db.placeholder),
                (connection_code,),
            ).fetchone()
        return self._row_to_session(row) if row is not None else None

    def mark_used(self, connection_code: str) -> None:
        with self._connect() as conn:
            conn.execute(self._mark_used_sql(), (connection_code,))

    @staticmethod
    def _row_to_session(row: Any) -> BitrixConnectSession:
        return BitrixConnectSession(
            connection_code=row["connection_code"],
            tenant_id=row["tenant_id"],
            bitrix_domain=row["bitrix_domain"],
            return_url=row["return_url"],
            expires_at=int(row["expires_at"] or 0),
            status=row["status"],
            used_at=row["used_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
