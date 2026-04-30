from __future__ import annotations

from pathlib import Path

from ...domain.business_profile import BusinessProfile
from .connection import Database

_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS business_profiles (
    tenant_id          TEXT PRIMARY KEY,
    company_name       TEXT NOT NULL DEFAULT '',
    website_url        TEXT NOT NULL DEFAULT '',
    instagram_url      TEXT NOT NULL DEFAULT '',
    price_list         TEXT NOT NULL DEFAULT '',
    average_ticket_kzt REAL,
    advantages         TEXT NOT NULL DEFAULT '',
    promotions         TEXT NOT NULL DEFAULT '',
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_DDL_POSTGRES = """
CREATE TABLE IF NOT EXISTS business_profiles (
    tenant_id          TEXT PRIMARY KEY,
    company_name       TEXT NOT NULL DEFAULT '',
    website_url        TEXT NOT NULL DEFAULT '',
    instagram_url      TEXT NOT NULL DEFAULT '',
    price_list         TEXT NOT NULL DEFAULT '',
    average_ticket_kzt DOUBLE PRECISION,
    advantages         TEXT NOT NULL DEFAULT '',
    promotions         TEXT NOT NULL DEFAULT '',
    updated_at         TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
)
"""

_SELECT = """
SELECT company_name, website_url, instagram_url, price_list,
       average_ticket_kzt, advantages, promotions
FROM business_profiles WHERE tenant_id = {p}
"""


class BusinessProfileRepository:
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
INSERT INTO business_profiles
    (tenant_id, company_name, website_url, instagram_url, price_list,
     average_ticket_kzt, advantages, promotions, updated_at)
VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {self._db.now_sql})
ON CONFLICT(tenant_id) DO UPDATE SET
    company_name       = excluded.company_name,
    website_url        = excluded.website_url,
    instagram_url      = excluded.instagram_url,
    price_list         = excluded.price_list,
    average_ticket_kzt = excluded.average_ticket_kzt,
    advantages         = excluded.advantages,
    promotions         = excluded.promotions,
    updated_at         = {self._db.now_sql}
"""

    def get(self) -> BusinessProfile | None:
        with self._connect() as conn:
            row = conn.execute(_SELECT.format(p=self._db.placeholder), (self._tenant_id,)).fetchone()
        if row is None:
            return None
        return BusinessProfile(
            company_name=row["company_name"],
            website_url=row["website_url"],
            instagram_url=row["instagram_url"],
            price_list=row["price_list"],
            average_ticket_kzt=row["average_ticket_kzt"],
            advantages=row["advantages"],
            promotions=row["promotions"],
        )

    def save(self, profile: BusinessProfile) -> None:
        with self._connect() as conn:
            conn.execute(self._upsert_sql(), (
                self._tenant_id,
                profile.company_name,
                profile.website_url,
                profile.instagram_url,
                profile.price_list,
                profile.average_ticket_kzt,
                profile.advantages,
                profile.promotions,
            ))
