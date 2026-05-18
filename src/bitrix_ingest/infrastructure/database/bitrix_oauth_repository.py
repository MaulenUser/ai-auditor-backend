from __future__ import annotations

from pathlib import Path
from typing import Any

from ...domain.bitrix_oauth import BitrixOAuthToken
from .connection import Database

_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS bitrix_oauth_tokens (
    tenant_id          TEXT PRIMARY KEY,
    bitrix_member_id  TEXT NOT NULL UNIQUE,
    bitrix_domain     TEXT NOT NULL DEFAULT '',
    client_endpoint   TEXT NOT NULL DEFAULT '',
    access_token      TEXT NOT NULL DEFAULT '',
    refresh_token     TEXT NOT NULL DEFAULT '',
    expires_at        INTEGER NOT NULL DEFAULT 0,
    scope             TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'active',
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_DDL_POSTGRES = """
CREATE TABLE IF NOT EXISTS bitrix_oauth_tokens (
    tenant_id          TEXT PRIMARY KEY,
    bitrix_member_id  TEXT NOT NULL UNIQUE,
    bitrix_domain     TEXT NOT NULL DEFAULT '',
    client_endpoint   TEXT NOT NULL DEFAULT '',
    access_token      TEXT NOT NULL DEFAULT '',
    refresh_token     TEXT NOT NULL DEFAULT '',
    expires_at        BIGINT NOT NULL DEFAULT 0,
    scope             TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'active',
    created_at        TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
    updated_at        TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
)
"""

_INDEX_MEMBER = """
CREATE INDEX IF NOT EXISTS idx_bitrix_oauth_tokens_member_id
ON bitrix_oauth_tokens(bitrix_member_id)
"""

_SELECT_COLUMNS = """
tenant_id, bitrix_member_id, bitrix_domain, client_endpoint,
access_token, refresh_token, expires_at, scope, status, created_at, updated_at
"""

_SELECT_BY_TENANT = f"""
SELECT {_SELECT_COLUMNS}
FROM bitrix_oauth_tokens
WHERE tenant_id = {{p}}
"""

_SELECT_BY_MEMBER = f"""
SELECT {_SELECT_COLUMNS}
FROM bitrix_oauth_tokens
WHERE bitrix_member_id = {{p}}
"""


class BitrixOAuthRepository:
    def __init__(self, db_path: Path) -> None:
        self._db = Database(db_path)
        with self._connect() as conn:
            conn.execute(_DDL_POSTGRES if self._db.is_postgres else _DDL_SQLITE)
            conn.execute(_INDEX_MEMBER)

    def _connect(self):
        return self._db.connect()

    def _upsert_sql(self) -> str:
        p = self._db.placeholder
        return f"""
INSERT INTO bitrix_oauth_tokens
    (tenant_id, bitrix_member_id, bitrix_domain, client_endpoint,
     access_token, refresh_token, expires_at, scope, status, updated_at)
VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {self._db.now_sql})
ON CONFLICT(tenant_id) DO UPDATE SET
    bitrix_member_id = excluded.bitrix_member_id,
    bitrix_domain    = excluded.bitrix_domain,
    client_endpoint  = excluded.client_endpoint,
    access_token     = excluded.access_token,
    refresh_token    = excluded.refresh_token,
    expires_at       = excluded.expires_at,
    scope            = excluded.scope,
    status           = excluded.status,
    updated_at       = {self._db.now_sql}
"""

    def _update_status_sql(self) -> str:
        p = self._db.placeholder
        return f"""
UPDATE bitrix_oauth_tokens
SET status = {p}, updated_at = {self._db.now_sql}
WHERE tenant_id = {p}
"""

    def _delete_sql(self) -> str:
        p = self._db.placeholder
        return f"DELETE FROM bitrix_oauth_tokens WHERE tenant_id = {p}"

    def save(self, token: BitrixOAuthToken) -> None:
        with self._connect() as conn:
            conn.execute(
                self._upsert_sql(),
                (
                    token.tenant_id,
                    token.bitrix_member_id,
                    token.bitrix_domain,
                    token.client_endpoint,
                    token.access_token,
                    token.refresh_token,
                    int(token.expires_at),
                    token.scope,
                    token.status,
                ),
            )

    def get_by_tenant(self, tenant_id: str) -> BitrixOAuthToken | None:
        with self._connect() as conn:
            row = conn.execute(
                _SELECT_BY_TENANT.format(p=self._db.placeholder),
                (tenant_id,),
            ).fetchone()
        return self._row_to_token(row) if row is not None else None

    def get_by_member_id(self, bitrix_member_id: str) -> BitrixOAuthToken | None:
        with self._connect() as conn:
            row = conn.execute(
                _SELECT_BY_MEMBER.format(p=self._db.placeholder),
                (bitrix_member_id,),
            ).fetchone()
        return self._row_to_token(row) if row is not None else None

    def update_status(self, tenant_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(self._update_status_sql(), (status, tenant_id))

    def delete_by_tenant(self, tenant_id: str) -> None:
        with self._connect() as conn:
            conn.execute(self._delete_sql(), (tenant_id,))

    @staticmethod
    def _row_to_token(row: Any) -> BitrixOAuthToken:
        return BitrixOAuthToken(
            tenant_id=row["tenant_id"],
            bitrix_member_id=row["bitrix_member_id"],
            bitrix_domain=row["bitrix_domain"],
            client_endpoint=row["client_endpoint"],
            access_token=row["access_token"],
            refresh_token=row["refresh_token"],
            expires_at=int(row["expires_at"] or 0),
            scope=row["scope"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
