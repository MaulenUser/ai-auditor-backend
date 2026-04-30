from __future__ import annotations

from pathlib import Path
from typing import Any

from ...domain.user import User
from .connection import Database

_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'client',
    password_hash TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_DDL_POSTGRES = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'client',
    password_hash TEXT NOT NULL,
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
    updated_at    TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
)
"""

_INDEX_TENANT = "CREATE INDEX IF NOT EXISTS idx_users_tenant_id ON users(tenant_id)"

_SELECT_ONE = """
SELECT username, tenant_id, role, password_hash, active, created_at
FROM users WHERE username = {p}
"""

_SELECT_ALL = """
SELECT username, tenant_id, role, password_hash, active, created_at
FROM users ORDER BY created_at
"""

_COUNT = "SELECT COUNT(*) AS n FROM users"


class UserRepository:
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
INSERT INTO users
    (username, tenant_id, role, password_hash, active, updated_at)
VALUES ({p}, {p}, {p}, {p}, {p}, {self._db.now_sql})
ON CONFLICT(username) DO UPDATE SET
    tenant_id     = excluded.tenant_id,
    role          = excluded.role,
    password_hash = excluded.password_hash,
    active        = excluded.active,
    updated_at    = {self._db.now_sql}
"""

    def get(self, username: str) -> User | None:
        with self._connect() as conn:
            row = conn.execute(_SELECT_ONE.format(p=self._db.placeholder), (username,)).fetchone()
        if row is None:
            return None
        return self._row_to_user(row)

    def list_all(self) -> list[User]:
        with self._connect() as conn:
            rows = conn.execute(_SELECT_ALL).fetchall()
        return [self._row_to_user(row) for row in rows]

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(_COUNT).fetchone()
        return int(row["n"] if row else 0)

    def save(self, user: User) -> None:
        with self._connect() as conn:
            conn.execute(
                self._upsert_sql(),
                (
                    user.username,
                    user.tenant_id,
                    user.role,
                    user.password_hash,
                    bool(user.active) if self._db.is_postgres else (1 if user.active else 0),
                ),
            )

    @staticmethod
    def _row_to_user(row: Any) -> User:
        return User(
            username=row["username"],
            tenant_id=row["tenant_id"],
            role=row["role"],
            password_hash=row["password_hash"],
            active=bool(row["active"]),
            created_at=row["created_at"],
        )
