"""Export a deal analytics snapshot from Bitrix24 into JSON and SQLite."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any

import requests


DEFAULT_SELECT = [
    "ID",
    "TITLE",
    "STAGE_ID",
    "STAGE_SEMANTIC_ID",
    "CATEGORY_ID",
    "ASSIGNED_BY_ID",
    "OPPORTUNITY",
    "CURRENCY_ID",
    "CONTACT_ID",
    "COMPANY_ID",
    "TYPE_ID",
    "SOURCE_ID",
    "SOURCE_DESCRIPTION",
    "UTM_SOURCE",
    "UTM_MEDIUM",
    "UTM_CAMPAIGN",
    "DATE_CREATE",
    "DATE_MODIFY",
    "BEGINDATE",
    "CLOSEDATE",
    "CLOSED",
]


@dataclass(frozen=True)
class Period:
    date_from: str
    date_to: str

    @property
    def start(self) -> str:
        return _bound(self.date_from, end_of_day=False)

    @property
    def end(self) -> str:
        return _bound(self.date_to, end_of_day=True)


class BitrixApi:
    def __init__(self, webhook_base_url: str, *, delay: float = 0.15) -> None:
        base = webhook_base_url.strip()
        if not base:
            raise ValueError("Webhook base URL is empty")
        self._base = base if base.endswith("/") else f"{base}/"
        self._session = requests.Session()
        self._delay = delay

    def call(self, method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._base}{method}.json"
        payload = body or {}
        last_error: Exception | None = None
        for attempt in range(1, 6):
            if self._delay > 0:
                time.sleep(self._delay)
            try:
                response = self._session.post(url, json=payload, timeout=60)
                if response.status_code in {429, 500, 502, 503, 504} and attempt < 5:
                    time.sleep(attempt * 1.5)
                    continue
                response.raise_for_status()
                data = response.json()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 5:
                    time.sleep(attempt * 1.5)
                    continue
                raise RuntimeError(f"Bitrix call failed: {method}") from exc

            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(
                    f"Bitrix call failed: {method}: {data.get('error')} {data.get('error_description')}"
                )
            return data
        raise RuntimeError(f"Bitrix call failed: {method}") from last_error

    def list_all(
        self,
        method: str,
        *,
        select: list[str] | None = None,
        filter_: dict[str, Any] | None = None,
        order: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        start = 0
        while True:
            payload: dict[str, Any] = {}
            if extra:
                payload.update(extra)
            if select is not None:
                payload["select"] = select
            if filter_ is not None:
                payload["filter"] = filter_
            if order is not None:
                payload["order"] = order
            payload["start"] = start

            data = self.call(method, payload)
            result = data.get("result")
            if result is None:
                break
            if isinstance(result, list):
                rows.extend(x for x in result if isinstance(x, dict))
            elif isinstance(result, dict):
                rows.append(result)
            next_start = data.get("next")
            if next_start is None:
                break
            start = int(next_start)
        return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export deal BI snapshot and build SQLite analytics tables."
    )
    parser.add_argument(
        "--webhook-base-url",
        default=os.environ.get("BITRIX_WEBHOOK_BASE_URL"),
        help="Bitrix24 webhook base URL, or BITRIX_WEBHOOK_BASE_URL env var.",
    )
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory. Default: export/sales-sql-<date-from>_<date-to>",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)
    if not args.webhook_base_url:
        raise SystemExit("ERROR: pass --webhook-base-url or set BITRIX_WEBHOOK_BASE_URL")

    period = Period(args.date_from, args.date_to)
    output_dir = Path(args.output_dir or f"export/sales-sql-{period.date_from}_{period.date_to}")
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    client = BitrixApi(args.webhook_base_url)

    print(f"Export period: {period.start} .. {period.end}")
    profile = client.call("profile")
    write_json(raw_dir / "profile.json", profile)

    users = _safe_paginated(client, "user.get", extra={})
    if not users:
        users_response = client.call("user.get")
        users = _as_list(users_response.get("result"))
    write_json(raw_dir / "users.json", users)

    departments = _safe_call_result_list(client, "department.get")
    write_json(raw_dir / "departments.json", departments)

    categories = _safe_call_result_list(client, "crm.dealcategory.list")
    if not any(_to_int(item.get("ID")) == 0 for item in categories):
        categories.insert(0, {"ID": "0", "NAME": "Default pipeline", "SORT": "0"})
    write_json(raw_dir / "deal_categories.json", categories)

    stages = export_stages(client, categories)
    write_json(raw_dir / "deal_stages.json", stages)

    active = client.list_all(
        "crm.deal.list",
        select=DEFAULT_SELECT,
        filter_={"CLOSED": "N", "<=DATE_CREATE": period.end},
        order={"ID": "ASC"},
    )
    closed = client.list_all(
        "crm.deal.list",
        select=DEFAULT_SELECT,
        filter_={"CLOSED": "Y", ">=CLOSEDATE": period.start, "<=CLOSEDATE": period.end},
        order={"ID": "ASC"},
    )
    created = client.list_all(
        "crm.deal.list",
        select=DEFAULT_SELECT,
        filter_={">=DATE_CREATE": period.start, "<=DATE_CREATE": period.end},
        order={"ID": "ASC"},
    )
    modified = client.list_all(
        "crm.deal.list",
        select=DEFAULT_SELECT,
        filter_={">=DATE_MODIFY": period.start, "<=DATE_MODIFY": period.end},
        order={"ID": "ASC"},
    )

    write_json(raw_dir / "deals_active_as_of_to.json", active)
    write_json(raw_dir / "deals_closed_in_period.json", closed)
    write_json(raw_dir / "deals_created_in_period.json", created)
    write_json(raw_dir / "deals_modified_in_period.json", modified)

    deals = merge_deals(
        {
            "active_as_of_to": active,
            "closed_in_period": closed,
            "created_in_period": created,
            "modified_in_period": modified,
        }
    )

    db_path = output_dir / "sales_analytics.sqlite"
    build_sqlite(
        db_path,
        period=period,
        deals=deals,
        users=users,
        departments=departments,
        categories=categories,
        stages=stages,
    )
    write_queries(output_dir / "queries.sql")

    summary = build_summary(db_path)
    summary.update(
        {
            "period": {"date_from": period.date_from, "date_to": period.date_to},
            "counts": {
                "deals_unique": len(deals),
                "active_as_of_to": len(active),
                "closed_in_period": len(closed),
                "created_in_period": len(created),
                "modified_in_period": len(modified),
                "users": len(users),
                "departments": len(departments),
                "categories": len(categories),
                "stages": len(stages),
            },
        }
    )
    write_json(output_dir / "summary.json", summary)
    write_markdown_summary(output_dir / "summary.md", summary)

    print(f"Rows: deals={len(deals)}, active={len(active)}, closed={len(closed)}, users={len(users)}")
    print(f"SQLite: {db_path}")
    print_summary(summary)


def export_stages(client: BitrixApi, categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stages: list[dict[str, Any]] = []
    default_stages = _safe_paginated(
        client,
        "crm.status.list",
        filter_={"ENTITY_ID": "DEAL_STAGE"},
        order={"SORT": "ASC"},
    )
    for stage in default_stages:
        item = dict(stage)
        item["_CATEGORY_ID"] = 0
        stages.append(item)

    for category in categories:
        category_id = _to_int(category.get("ID"))
        if category_id in {None, 0}:
            continue
        for stage in _safe_call_result_list(
            client,
            "crm.dealcategory.stage.list",
            body={"id": category_id},
        ):
            item = dict(stage)
            item["_CATEGORY_ID"] = category_id
            stages.append(item)
    return stages


def merge_deals(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for flag, rows in groups.items():
        for row in rows:
            deal_id = str(row.get("ID") or "").strip()
            if not deal_id:
                continue
            current = by_id.setdefault(deal_id, dict(row))
            current[f"_{flag}"] = True
            if len(row) > len(current):
                for key, value in row.items():
                    current.setdefault(key, value)
    for row in by_id.values():
        for flag in groups:
            row.setdefault(f"_{flag}", False)
    return sorted(by_id.values(), key=lambda item: _to_int(item.get("ID")) or 0)


def build_sqlite(
    db_path: Path,
    *,
    period: Period,
    deals: list[dict[str, Any]],
    users: list[dict[str, Any]],
    departments: list[dict[str, Any]],
    categories: list[dict[str, Any]],
    stages: list[dict[str, Any]],
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        for name in [
            "deals",
            "users",
            "user_departments",
            "departments",
            "deal_categories",
            "deal_stages",
            "snapshot_meta",
        ]:
            conn.execute(f"DROP TABLE IF EXISTS {name}")
        conn.execute("DROP VIEW IF EXISTS v_deals_enriched")

        conn.execute(
            """
            CREATE TABLE snapshot_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO snapshot_meta(key, value) VALUES (?, ?)",
            [
                ("date_from", period.date_from),
                ("date_to", period.date_to),
                ("period_start", period.start),
                ("period_end", period.end),
            ],
        )

        conn.execute(
            """
            CREATE TABLE deals (
                id INTEGER PRIMARY KEY,
                title TEXT,
                category_id INTEGER,
                stage_id TEXT,
                stage_semantic_id TEXT,
                assigned_by_id INTEGER,
                opportunity REAL,
                currency_id TEXT,
                contact_id INTEGER,
                company_id INTEGER,
                type_id TEXT,
                source_id TEXT,
                source_description TEXT,
                utm_source TEXT,
                utm_medium TEXT,
                utm_campaign TEXT,
                date_create TEXT,
                date_modify TEXT,
                begin_date TEXT,
                close_date TEXT,
                closed TEXT,
                active_as_of_to INTEGER NOT NULL,
                closed_in_period INTEGER NOT NULL,
                created_in_period INTEGER NOT NULL,
                modified_in_period INTEGER NOT NULL,
                raw_json TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO deals (
                id, title, category_id, stage_id, stage_semantic_id, assigned_by_id,
                opportunity, currency_id, contact_id, company_id, type_id, source_id,
                source_description, utm_source, utm_medium, utm_campaign, date_create,
                date_modify, begin_date, close_date, closed, active_as_of_to,
                closed_in_period, created_in_period, modified_in_period, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [_deal_row(item) for item in deals],
        )

        conn.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                name TEXT,
                second_name TEXT,
                last_name TEXT,
                active TEXT,
                work_position TEXT,
                primary_department_id INTEGER,
                raw_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE user_departments (
                user_id INTEGER NOT NULL,
                department_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                PRIMARY KEY (user_id, department_id, ordinal)
            )
            """
        )
        for user in users:
            user_id = _to_int(user.get("ID"))
            if user_id is None:
                continue
            department_ids = _department_ids(user.get("UF_DEPARTMENT"))
            conn.execute(
                """
                INSERT OR REPLACE INTO users (
                    id, name, second_name, last_name, active, work_position,
                    primary_department_id, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    _clean(user.get("NAME")),
                    _clean(user.get("SECOND_NAME")),
                    _clean(user.get("LAST_NAME")),
                    _clean(user.get("ACTIVE")),
                    _clean(user.get("WORK_POSITION")),
                    department_ids[0] if department_ids else None,
                    json.dumps(user, ensure_ascii=False, sort_keys=True),
                ),
            )
            for ordinal, department_id in enumerate(department_ids):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO user_departments(user_id, department_id, ordinal)
                    VALUES (?, ?, ?)
                    """,
                    (user_id, department_id, ordinal),
                )

        conn.execute(
            """
            CREATE TABLE departments (
                id INTEGER PRIMARY KEY,
                name TEXT,
                parent_id INTEGER,
                sort INTEGER,
                raw_json TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO departments(id, name, parent_id, sort, raw_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    _to_int(item.get("ID")),
                    _clean(item.get("NAME")),
                    _to_int(item.get("PARENT")),
                    _to_int(item.get("SORT")),
                    json.dumps(item, ensure_ascii=False, sort_keys=True),
                )
                for item in departments
                if _to_int(item.get("ID")) is not None
            ],
        )

        conn.execute(
            """
            CREATE TABLE deal_categories (
                id INTEGER PRIMARY KEY,
                name TEXT,
                sort INTEGER,
                raw_json TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO deal_categories(id, name, sort, raw_json)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    _to_int(item.get("ID")),
                    _clean(item.get("NAME")),
                    _to_int(item.get("SORT")),
                    json.dumps(item, ensure_ascii=False, sort_keys=True),
                )
                for item in categories
                if _to_int(item.get("ID")) is not None
            ],
        )

        conn.execute(
            """
            CREATE TABLE deal_stages (
                category_id INTEGER NOT NULL,
                status_id TEXT NOT NULL,
                name TEXT,
                sort INTEGER,
                semantics TEXT,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (category_id, status_id)
            )
            """
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO deal_stages(
                category_id, status_id, name, sort, semantics, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    _to_int(item.get("_CATEGORY_ID")) or 0,
                    str(item.get("STATUS_ID") or item.get("ID") or ""),
                    _clean(item.get("NAME")),
                    _to_int(item.get("SORT")),
                    _clean(item.get("SEMANTICS")),
                    json.dumps(item, ensure_ascii=False, sort_keys=True),
                )
                for item in stages
                if str(item.get("STATUS_ID") or item.get("ID") or "").strip()
            ],
        )

        conn.execute(
            """
            CREATE VIEW v_deals_enriched AS
            SELECT
                d.*,
                CASE
                    WHEN d.stage_semantic_id = 'S' THEN 'won'
                    WHEN d.stage_semantic_id = 'F' THEN 'lost'
                    WHEN d.closed = 'Y' THEN 'closed_other'
                    WHEN d.closed = 'N' OR d.stage_semantic_id = 'P' THEN 'in_work'
                    ELSE 'other'
                END AS status_bucket,
                COALESCE(NULLIF(TRIM(COALESCE(u.last_name, '') || ' ' || COALESCE(u.name, '')), ''), 'User #' || d.assigned_by_id) AS manager_name,
                COALESCE(dep.name, 'No department') AS department_name,
                COALESCE(cat.name, CASE WHEN d.category_id = 0 THEN 'Default pipeline' ELSE 'Pipeline #' || d.category_id END) AS pipeline_name,
                st.name AS stage_name
            FROM deals d
            LEFT JOIN users u ON u.id = d.assigned_by_id
            LEFT JOIN departments dep ON dep.id = u.primary_department_id
            LEFT JOIN deal_categories cat ON cat.id = d.category_id
            LEFT JOIN deal_stages st ON st.category_id = COALESCE(d.category_id, 0)
                AND st.status_id = d.stage_id
            """
        )

        conn.execute("CREATE INDEX IF NOT EXISTS idx_deals_assigned ON deals(assigned_by_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deals_closed_period ON deals(closed_in_period)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deals_active ON deals(active_as_of_to)")
        conn.commit()


def build_summary(db_path: Path) -> dict[str, Any]:
    queries = {
        "totals": """
            SELECT
                COUNT(*) AS deals_loaded,
                SUM(active_as_of_to) AS active_as_of_to,
                SUM(CASE WHEN closed_in_period = 1 AND status_bucket = 'won' THEN 1 ELSE 0 END) AS won_count,
                ROUND(SUM(CASE WHEN closed_in_period = 1 AND status_bucket = 'won' THEN opportunity ELSE 0 END), 2) AS won_amount,
                SUM(CASE WHEN closed_in_period = 1 AND status_bucket = 'lost' THEN 1 ELSE 0 END) AS lost_count,
                ROUND(SUM(CASE WHEN closed_in_period = 1 AND status_bucket = 'lost' THEN opportunity ELSE 0 END), 2) AS lost_amount,
                SUM(CASE WHEN closed_in_period = 1 AND status_bucket NOT IN ('won', 'lost') THEN 1 ELSE 0 END) AS closed_other_count
            FROM v_deals_enriched
        """,
        "active_by_department": """
            SELECT department_name, COUNT(*) AS deals_count, ROUND(SUM(opportunity), 2) AS amount
            FROM v_deals_enriched
            WHERE active_as_of_to = 1
            GROUP BY department_name
            ORDER BY deals_count DESC, amount DESC
        """,
        "active_by_manager": """
            SELECT department_name, manager_name, COUNT(*) AS deals_count, ROUND(SUM(opportunity), 2) AS amount
            FROM v_deals_enriched
            WHERE active_as_of_to = 1
            GROUP BY department_name, manager_name
            ORDER BY deals_count DESC, amount DESC
        """,
        "won_by_department_manager": """
            SELECT department_name, manager_name, COUNT(*) AS deals_count, ROUND(SUM(opportunity), 2) AS amount
            FROM v_deals_enriched
            WHERE closed_in_period = 1 AND status_bucket = 'won'
            GROUP BY department_name, manager_name
            ORDER BY amount DESC, deals_count DESC
        """,
        "lost_by_department_manager": """
            SELECT department_name, manager_name, COUNT(*) AS deals_count, ROUND(SUM(opportunity), 2) AS amount
            FROM v_deals_enriched
            WHERE closed_in_period = 1 AND status_bucket = 'lost'
            GROUP BY department_name, manager_name
            ORDER BY deals_count DESC, amount DESC
        """,
    }
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return {name: [dict(row) for row in conn.execute(sql)] for name, sql in queries.items()}


def write_queries(path: Path) -> None:
    path.write_text(
        """
-- Totals for the requested period and snapshot.
SELECT
    COUNT(*) AS deals_loaded,
    SUM(active_as_of_to) AS active_as_of_to,
    SUM(CASE WHEN closed_in_period = 1 AND status_bucket = 'won' THEN 1 ELSE 0 END) AS won_count,
    ROUND(SUM(CASE WHEN closed_in_period = 1 AND status_bucket = 'won' THEN opportunity ELSE 0 END), 2) AS won_amount,
    SUM(CASE WHEN closed_in_period = 1 AND status_bucket = 'lost' THEN 1 ELSE 0 END) AS lost_count,
    ROUND(SUM(CASE WHEN closed_in_period = 1 AND status_bucket = 'lost' THEN opportunity ELSE 0 END), 2) AS lost_amount,
    SUM(CASE WHEN closed_in_period = 1 AND status_bucket NOT IN ('won', 'lost') THEN 1 ELSE 0 END) AS closed_other_count
FROM v_deals_enriched;

-- Active deals as of DATE_TO by department and manager.
SELECT department_name, manager_name, COUNT(*) AS deals_count, ROUND(SUM(opportunity), 2) AS amount
FROM v_deals_enriched
WHERE active_as_of_to = 1
GROUP BY department_name, manager_name
ORDER BY deals_count DESC, amount DESC;

-- Won deals closed in the period by department and manager.
SELECT department_name, manager_name, COUNT(*) AS deals_count, ROUND(SUM(opportunity), 2) AS amount
FROM v_deals_enriched
WHERE closed_in_period = 1 AND status_bucket = 'won'
GROUP BY department_name, manager_name
ORDER BY amount DESC, deals_count DESC;

-- Lost deals closed in the period by department and manager.
SELECT department_name, manager_name, COUNT(*) AS deals_count, ROUND(SUM(opportunity), 2) AS amount
FROM v_deals_enriched
WHERE closed_in_period = 1 AND status_bucket = 'lost'
GROUP BY department_name, manager_name
ORDER BY deals_count DESC, amount DESC;
""".lstrip(),
        encoding="utf-8",
    )


def write_markdown_summary(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Sales SQL snapshot",
        "",
        f"Period: {summary['period']['date_from']} .. {summary['period']['date_to']}",
        "",
        "## Counts",
    ]
    for key, value in summary["counts"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Totals"])
    totals = summary["totals"][0] if summary.get("totals") else {}
    for key, value in totals.items():
        lines.append(f"- {key}: {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_summary(summary: dict[str, Any]) -> None:
    totals = summary["totals"][0] if summary.get("totals") else {}
    print("Totals:")
    for key in ["active_as_of_to", "won_count", "won_amount", "lost_count", "lost_amount"]:
        print(f"  {key}: {totals.get(key)}")
    print("Top active managers:")
    for row in summary.get("active_by_manager", [])[:10]:
        print(
            f"  {row.get('department_name')} | {row.get('manager_name')}: "
            f"{row.get('deals_count')} deals, {row.get('amount')}"
        )


def _deal_row(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _to_int(item.get("ID")),
        _clean(item.get("TITLE")),
        _to_int(item.get("CATEGORY_ID")) or 0,
        _clean(item.get("STAGE_ID")),
        _clean(item.get("STAGE_SEMANTIC_ID")),
        _to_int(item.get("ASSIGNED_BY_ID")),
        _to_float(item.get("OPPORTUNITY")),
        _clean(item.get("CURRENCY_ID")),
        _to_int(item.get("CONTACT_ID")),
        _to_int(item.get("COMPANY_ID")),
        _clean(item.get("TYPE_ID")),
        _clean(item.get("SOURCE_ID")),
        _clean(item.get("SOURCE_DESCRIPTION")),
        _clean(item.get("UTM_SOURCE")),
        _clean(item.get("UTM_MEDIUM")),
        _clean(item.get("UTM_CAMPAIGN")),
        _clean(item.get("DATE_CREATE")),
        _clean(item.get("DATE_MODIFY")),
        _clean(item.get("BEGINDATE")),
        _clean(item.get("CLOSEDATE")),
        _clean(item.get("CLOSED")),
        int(bool(item.get("_active_as_of_to"))),
        int(bool(item.get("_closed_in_period"))),
        int(bool(item.get("_created_in_period"))),
        int(bool(item.get("_modified_in_period"))),
        json.dumps(item, ensure_ascii=False, sort_keys=True),
    )


def _safe_call_result_list(
    client: BitrixApi,
    method: str,
    body: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    try:
        return _as_list(client.call(method, body or {}).get("result"))
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: skipped {method}: {exc}")
        return []


def _safe_paginated(
    client: BitrixApi,
    method: str,
    *,
    select: list[str] | None = None,
    filter_: dict[str, Any] | None = None,
    order: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    try:
        return client.list_all(method, select=select, filter_=filter_, order=order, extra=extra)
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: pagination skipped for {method}: {exc}")
        return []


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _department_ids(value: Any) -> list[int]:
    if value is None:
        return []
    values: Iterable[Any]
    if isinstance(value, list):
        values = value
    else:
        values = [value]
    result: list[int] = []
    for raw in values:
        parsed = _to_int(raw)
        if parsed is not None:
            result.append(parsed)
    return result


def _bound(value: str, *, end_of_day: bool) -> str:
    text = value.strip()
    if len(text) == 10:
        parsed = datetime.fromisoformat(text)
        t = dt_time(23, 59, 59) if end_of_day else dt_time(0, 0, 0)
        return datetime.combine(parsed.date(), t).isoformat()
    return text


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).replace(" ", "").replace(",", ".")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
