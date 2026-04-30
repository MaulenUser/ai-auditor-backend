from __future__ import annotations

from pathlib import Path

from ...domain.integrations import Integrations
from .connection import Database

_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS tenant_integrations (
    tenant_id            TEXT PRIMARY KEY,
    bitrix_webhook_url   TEXT NOT NULL DEFAULT '',
    whatsapp_webhook_url TEXT NOT NULL DEFAULT '',
    openai_api_key       TEXT NOT NULL DEFAULT '',
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_DDL_POSTGRES = """
CREATE TABLE IF NOT EXISTS tenant_integrations (
    tenant_id            TEXT PRIMARY KEY,
    bitrix_webhook_url   TEXT NOT NULL DEFAULT '',
    whatsapp_webhook_url TEXT NOT NULL DEFAULT '',
    openai_api_key       TEXT NOT NULL DEFAULT '',
    updated_at           TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
)
"""

_SELECT = """
SELECT bitrix_webhook_url, whatsapp_webhook_url, openai_api_key
FROM tenant_integrations WHERE tenant_id = {p}
"""


class IntegrationsRepository:
    def __init__(self, db_path: Path, tenant_id: str = "default") -> None:
        self._db = Database(db_path)
        self._tenant_id = tenant_id
        with self._connect() as conn:
            conn.execute(_DDL_POSTGRES if self._db.is_postgres else _DDL_SQLITE)

    def _connect(self):
        return self._db.connect()

    def _upsert_sql(self) -> str:
        p = self._db.placeholder
        return f"""
INSERT INTO tenant_integrations
    (tenant_id, bitrix_webhook_url, whatsapp_webhook_url, openai_api_key, updated_at)
VALUES ({p}, {p}, {p}, {p}, {self._db.now_sql})
ON CONFLICT(tenant_id) DO UPDATE SET
    bitrix_webhook_url   = excluded.bitrix_webhook_url,
    whatsapp_webhook_url = excluded.whatsapp_webhook_url,
    openai_api_key       = excluded.openai_api_key,
    updated_at           = {self._db.now_sql}
"""

    def get(self) -> Integrations | None:
        with self._connect() as conn:
            row = conn.execute(_SELECT.format(p=self._db.placeholder), (self._tenant_id,)).fetchone()
        if row is None:
            return None
        return Integrations(
            bitrix_webhook_url=row["bitrix_webhook_url"],
            whatsapp_webhook_url=row["whatsapp_webhook_url"],
            openai_api_key=row["openai_api_key"],
        )

    def save(self, integrations: Integrations) -> None:
        with self._connect() as conn:
            conn.execute(self._upsert_sql(), (
                self._tenant_id,
                integrations.bitrix_webhook_url,
                integrations.whatsapp_webhook_url,
                integrations.openai_api_key,
            ))
