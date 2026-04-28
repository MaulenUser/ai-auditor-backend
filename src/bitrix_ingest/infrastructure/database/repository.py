from __future__ import annotations

import sqlite3
from pathlib import Path

from ...domain.business_profile import BusinessProfile

_DDL = """
CREATE TABLE IF NOT EXISTS business_profile (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    company_name     TEXT NOT NULL DEFAULT '',
    website_url      TEXT NOT NULL DEFAULT '',
    instagram_url    TEXT NOT NULL DEFAULT '',
    price_list       TEXT NOT NULL DEFAULT '',
    average_ticket_kzt REAL,
    advantages       TEXT NOT NULL DEFAULT '',
    promotions       TEXT NOT NULL DEFAULT '',
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_UPSERT = """
INSERT INTO business_profile
    (id, company_name, website_url, instagram_url, price_list,
     average_ticket_kzt, advantages, promotions, updated_at)
VALUES (1, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
ON CONFLICT(id) DO UPDATE SET
    company_name       = excluded.company_name,
    website_url        = excluded.website_url,
    instagram_url      = excluded.instagram_url,
    price_list         = excluded.price_list,
    average_ticket_kzt = excluded.average_ticket_kzt,
    advantages         = excluded.advantages,
    promotions         = excluded.promotions,
    updated_at         = datetime('now')
"""

_SELECT = """
SELECT company_name, website_url, instagram_url, price_list,
       average_ticket_kzt, advantages, promotions
FROM business_profile WHERE id = 1
"""


class BusinessProfileRepository:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_DDL)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self) -> BusinessProfile | None:
        with self._connect() as conn:
            row = conn.execute(_SELECT).fetchone()
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
            conn.execute(_UPSERT, (
                profile.company_name,
                profile.website_url,
                profile.instagram_url,
                profile.price_list,
                profile.average_ticket_kzt,
                profile.advantages,
                profile.promotions,
            ))
