from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .connection import Database


_DDL_POSTGRES = """
CREATE TABLE IF NOT EXISTS sales_analytics_deals (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    title TEXT,
    category_id INTEGER,
    pipeline_name TEXT,
    stage_id TEXT,
    stage_name TEXT,
    stage_semantic_id TEXT,
    assigned_by_id INTEGER,
    manager_name TEXT,
    department_name TEXT,
    opportunity DOUBLE PRECISION,
    currency_id TEXT,
    contact_id INTEGER,
    company_id INTEGER,
    type_id TEXT,
    source_id TEXT,
    source_description TEXT,
    date_create TEXT,
    date_modify TEXT,
    begin_date TEXT,
    close_date TEXT,
    closed TEXT,
    active_as_of_to INTEGER NOT NULL DEFAULT 0,
    closed_in_period INTEGER NOT NULL DEFAULT 0,
    created_in_period INTEGER NOT NULL DEFAULT 0,
    modified_in_period INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id, id)
);
CREATE TABLE IF NOT EXISTS sales_analytics_tasks (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    title TEXT,
    status INTEGER,
    deadline TEXT,
    deadline_utc TEXT,
    closed_date TEXT,
    closed_date_utc TEXT,
    responsible_id INTEGER,
    created_by INTEGER,
    created_date TEXT,
    changed_date TEXT,
    is_completed INTEGER NOT NULL DEFAULT 0,
    is_overdue INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id, id)
);
CREATE TABLE IF NOT EXISTS sales_analytics_task_bindings (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    raw_value TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id, task_id, entity_type, entity_id, raw_value)
);
CREATE TABLE IF NOT EXISTS sales_analytics_leads (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    title TEXT,
    status_id TEXT,
    status_name TEXT,
    status_semantic_id TEXT,
    status_description TEXT,
    assigned_by_id INTEGER,
    manager_name TEXT,
    opportunity DOUBLE PRECISION,
    currency_id TEXT,
    source_id TEXT,
    source_description TEXT,
    date_create TEXT,
    date_modify TEXT,
    date_closed TEXT,
    comments TEXT,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id, id)
);
CREATE TABLE IF NOT EXISTS sales_analytics_revenue_documents (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    source TEXT NOT NULL,
    document_id TEXT NOT NULL,
    account_number TEXT,
    order_id TEXT,
    status_id TEXT,
    paid TEXT,
    date_create TEXT,
    date_update TEXT,
    date_paid TEXT,
    amount DOUBLE PRECISION,
    paid_amount DOUBLE PRECISION,
    currency_id TEXT,
    responsible_id INTEGER,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id, source, document_id)
);
CREATE TABLE IF NOT EXISTS sales_analytics_run_meta (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id, key)
);
CREATE TABLE IF NOT EXISTS sales_audit_reports (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    report_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
    hidden_at TEXT,
    PRIMARY KEY (tenant_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_sales_analytics_deals_scope
    ON sales_analytics_deals(tenant_id, run_id, active_as_of_to, closed_in_period, stage_semantic_id, assigned_by_id);
CREATE INDEX IF NOT EXISTS idx_sales_analytics_bindings_entity
    ON sales_analytics_task_bindings(tenant_id, run_id, entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_sales_analytics_leads_scope
    ON sales_analytics_leads(tenant_id, run_id, status_semantic_id, assigned_by_id);
CREATE INDEX IF NOT EXISTS idx_sales_analytics_revenue_scope
    ON sales_analytics_revenue_documents(tenant_id, run_id, source, paid);
"""

_SALES_AUDIT_REPORT_COLUMNS_POSTGRES = {
    "hidden_at": "TEXT",
}


class SalesAnalyticsRepository:
    """PostgreSQL storage for sales analytics snapshots and final reports."""

    def __init__(self, db_path: Path) -> None:
        self._db = Database(db_path)
        if not self._db.is_postgres:
            raise RuntimeError(
                "Sales analytics requires PostgreSQL. Set BITRIX_DATABASE_URL "
                "or DATABASE_URL; SQLite storage is disabled for sales analytics."
            )
        with self._connect() as conn:
            for statement in [part.strip() for part in _DDL_POSTGRES.split(";") if part.strip()]:
                conn.execute(statement)
            self._ensure_sales_audit_report_columns(conn)

    def _connect(self):
        return self._db.connect()

    @property
    def _p(self) -> str:
        return self._db.placeholder

    def _ensure_sales_audit_report_columns(self, conn: Any) -> None:
        for name, definition in _SALES_AUDIT_REPORT_COLUMNS_POSTGRES.items():
            conn.execute(
                f"ALTER TABLE sales_audit_reports ADD COLUMN IF NOT EXISTS {name} {definition}"
            )

    def replace_snapshot(
        self,
        *,
        tenant_id: str,
        run_id: str,
        meta: dict[str, Any],
        deals: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
        task_bindings: list[dict[str, Any]],
        leads: list[dict[str, Any]] | None = None,
        revenue_documents: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        p = self._p
        leads = leads or []
        revenue_documents = revenue_documents or []
        with self._connect() as conn:
            for table in (
                "sales_analytics_task_bindings",
                "sales_analytics_tasks",
                "sales_analytics_leads",
                "sales_analytics_revenue_documents",
                "sales_analytics_deals",
                "sales_analytics_run_meta",
            ):
                conn.execute(
                    f"DELETE FROM {table} WHERE tenant_id = {p} AND run_id = {p}",
                    (tenant_id, run_id),
                )

            for key, value in meta.items():
                conn.execute(
                    f"""
                    INSERT INTO sales_analytics_run_meta(tenant_id, run_id, key, value)
                    VALUES ({p}, {p}, {p}, {p})
                    """,
                    (tenant_id, run_id, str(key), str(value)),
                )

            for deal in deals:
                conn.execute(
                    f"""
                    INSERT INTO sales_analytics_deals (
                        tenant_id, run_id, id, title, category_id, pipeline_name,
                        stage_id, stage_name, stage_semantic_id, assigned_by_id,
                        manager_name, department_name, opportunity, currency_id,
                        contact_id, company_id, type_id, source_id,
                        source_description, date_create, date_modify, begin_date,
                        close_date, closed, active_as_of_to, closed_in_period,
                        created_in_period, modified_in_period, raw_json
                    )
                    VALUES (
                        {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p},
                        {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p},
                        {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}
                    )
                    """,
                    (
                        tenant_id,
                        run_id,
                        deal["id"],
                        deal.get("title", ""),
                        deal.get("category_id"),
                        deal.get("pipeline_name", ""),
                        deal.get("stage_id", ""),
                        deal.get("stage_name", ""),
                        deal.get("stage_semantic_id", ""),
                        deal.get("assigned_by_id"),
                        deal.get("manager_name", ""),
                        deal.get("department_name", ""),
                        deal.get("opportunity", 0.0),
                        deal.get("currency_id", ""),
                        deal.get("contact_id"),
                        deal.get("company_id"),
                        deal.get("type_id", ""),
                        deal.get("source_id", ""),
                        deal.get("source_description", ""),
                        deal.get("date_create", ""),
                        deal.get("date_modify", ""),
                        deal.get("begin_date", ""),
                        deal.get("close_date", ""),
                        deal.get("closed", ""),
                        deal.get("active_as_of_to", 0),
                        deal.get("closed_in_period", 0),
                        deal.get("created_in_period", 0),
                        deal.get("modified_in_period", 0),
                        json.dumps(deal.get("raw") or {}, ensure_ascii=False, default=str),
                    ),
                )

            for task in tasks:
                conn.execute(
                    f"""
                    INSERT INTO sales_analytics_tasks (
                        tenant_id, run_id, id, title, status, deadline,
                        deadline_utc, closed_date, closed_date_utc,
                        responsible_id, created_by, created_date, changed_date,
                        is_completed, is_overdue, raw_json
                    )
                    VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
                    """,
                    (
                        tenant_id,
                        run_id,
                        task["id"],
                        task.get("title", ""),
                        task.get("status"),
                        task.get("deadline", ""),
                        task.get("deadline_utc", ""),
                        task.get("closed_date", ""),
                        task.get("closed_date_utc", ""),
                        task.get("responsible_id"),
                        task.get("created_by"),
                        task.get("created_date", ""),
                        task.get("changed_date", ""),
                        task.get("is_completed", 0),
                        task.get("is_overdue", 0),
                        json.dumps(task.get("raw") or {}, ensure_ascii=False, default=str),
                    ),
                )

            for binding in task_bindings:
                conn.execute(
                    f"""
                    INSERT INTO sales_analytics_task_bindings (
                        tenant_id, run_id, task_id, entity_type, entity_id, raw_value
                    )
                    VALUES ({p}, {p}, {p}, {p}, {p}, {p})
                    """,
                    (
                        tenant_id,
                        run_id,
                        binding["task_id"],
                        binding["entity_type"],
                        binding["entity_id"],
                        binding.get("raw_value", ""),
                    ),
                )

            for lead in leads:
                conn.execute(
                    f"""
                    INSERT INTO sales_analytics_leads (
                        tenant_id, run_id, id, title, status_id, status_name,
                        status_semantic_id, status_description, assigned_by_id,
                        manager_name, opportunity, currency_id, source_id,
                        source_description, date_create, date_modify, date_closed,
                        comments, raw_json
                    )
                    VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
                    """,
                    (
                        tenant_id,
                        run_id,
                        lead["id"],
                        lead.get("title", ""),
                        lead.get("status_id", ""),
                        lead.get("status_name", ""),
                        lead.get("status_semantic_id", ""),
                        lead.get("status_description", ""),
                        lead.get("assigned_by_id"),
                        lead.get("manager_name", ""),
                        lead.get("opportunity", 0.0),
                        lead.get("currency_id", ""),
                        lead.get("source_id", ""),
                        lead.get("source_description", ""),
                        lead.get("date_create", ""),
                        lead.get("date_modify", ""),
                        lead.get("date_closed", ""),
                        lead.get("comments", ""),
                        json.dumps(lead.get("raw") or {}, ensure_ascii=False, default=str),
                    ),
                )

            for doc in revenue_documents:
                conn.execute(
                    f"""
                    INSERT INTO sales_analytics_revenue_documents (
                        tenant_id, run_id, source, document_id, account_number,
                        order_id, status_id, paid, date_create, date_update,
                        date_paid, amount, paid_amount, currency_id,
                        responsible_id, raw_json
                    )
                    VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
                    """,
                    (
                        tenant_id,
                        run_id,
                        doc.get("source", ""),
                        str(doc.get("document_id") or ""),
                        doc.get("account_number", ""),
                        doc.get("order_id", ""),
                        doc.get("status_id", ""),
                        doc.get("paid", ""),
                        doc.get("date_create", ""),
                        doc.get("date_update", ""),
                        doc.get("date_paid", ""),
                        doc.get("amount", 0.0),
                        doc.get("paid_amount", 0.0),
                        doc.get("currency_id", ""),
                        doc.get("responsible_id"),
                        json.dumps(doc.get("raw") or {}, ensure_ascii=False, default=str),
                    ),
                )

            self._analyze_snapshot_tables(conn)

        return self.build_report(tenant_id=tenant_id, run_id=run_id)

    def _analyze_snapshot_tables(self, conn: Any) -> None:
        if not getattr(self._db, "is_postgres", False):
            return

        for table in (
            "sales_analytics_deals",
            "sales_analytics_tasks",
            "sales_analytics_task_bindings",
            "sales_analytics_leads",
            "sales_analytics_revenue_documents",
        ):
            conn.execute(f"ANALYZE {table}")

    def build_report(self, *, tenant_id: str, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            return {
                "tenant_id": tenant_id,
                "run_id": run_id,
                "meta": self._meta(conn, tenant_id, run_id),
                "deal_dashboard": self._deal_dashboard(conn, tenant_id, run_id),
                "task_status": self._task_status(conn, tenant_id, run_id),
                "lead_status": self._lead_status(conn, tenant_id, run_id),
                "revenue_summary": self._revenue_summary(conn, tenant_id, run_id),
                "failure_reasons": self._failure_reasons(conn, tenant_id, run_id),
                "references": self._references(conn, tenant_id, run_id),
            }

    def save_sales_audit_report(
        self,
        *,
        tenant_id: str,
        run_id: str,
        report: dict[str, Any],
    ) -> None:
        p = self._p
        summary = {
            "generated_at": report.get("generated_at"),
            "scope": report.get("scope"),
            "integral_rating": report.get("integral_rating"),
            "deal_dashboard": report.get("deal_dashboard"),
            "task_status": report.get("task_status"),
            "lead_status": report.get("lead_status"),
            "revenue_summary": report.get("revenue_summary"),
            "data_readiness": report.get("data_readiness"),
            "sources": report.get("sales_audit_sources"),
        }
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO sales_audit_reports(tenant_id, run_id, report_json, summary_json, updated_at, hidden_at)
                VALUES ({p}, {p}, {p}, {p}, {self._db.now_sql}, NULL)
                ON CONFLICT(tenant_id, run_id) DO UPDATE SET
                    report_json = excluded.report_json,
                    summary_json = excluded.summary_json,
                    updated_at = {self._db.now_sql},
                    hidden_at = NULL
                """,
                (
                    tenant_id,
                    run_id,
                    json.dumps(report, ensure_ascii=False, default=str),
                    json.dumps(summary, ensure_ascii=False, default=str),
                ),
            )

    def get_sales_audit_report(self, *, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        p = self._p
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT report_json
                FROM sales_audit_reports
                WHERE tenant_id = {p} AND run_id = {p} AND hidden_at IS NULL
                """,
                (tenant_id, run_id),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["report_json"])

    def list_sales_audit_reports(self, *, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        p = self._p
        safe_limit = max(1, min(int(limit or 50), 200))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT run_id, summary_json, updated_at
                FROM sales_audit_reports
                WHERE tenant_id = {p} AND hidden_at IS NULL
                ORDER BY updated_at DESC
                LIMIT {p}
                """,
                (tenant_id, safe_limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                summary = json.loads(row["summary_json"] or "{}")
            except json.JSONDecodeError:
                summary = {}
            result.append({
                "run_id": row["run_id"],
                "updated_at": row["updated_at"],
                "summary": summary,
            })
        return result

    def hide_sales_audit_report(self, *, tenant_id: str, run_id: str) -> bool:
        p = self._p
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE sales_audit_reports
                SET hidden_at = COALESCE(hidden_at, {self._db.now_sql})
                WHERE tenant_id = {p} AND run_id = {p} AND hidden_at IS NULL
                """,
                (tenant_id, run_id),
            )
            return bool(cursor.rowcount)

    def _deal_dashboard(self, conn: Any, tenant_id: str, run_id: str) -> dict[str, Any]:
        p = self._p
        where = f"tenant_id = {p} AND run_id = {p}"
        department = _normalize_dashboard_row(
            conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total_deals,
                    SUM(opportunity) AS total_amount,
                    SUM(CASE WHEN active_as_of_to = 1 THEN 1 ELSE 0 END) AS in_work_count,
                    SUM(CASE WHEN active_as_of_to = 1 THEN opportunity ELSE 0 END) AS in_work_amount,
                    SUM(CASE WHEN closed_in_period = 1 AND stage_semantic_id = 'S' THEN 1 ELSE 0 END) AS won_count,
                    SUM(CASE WHEN closed_in_period = 1 AND stage_semantic_id = 'S' THEN opportunity ELSE 0 END) AS won_amount,
                    SUM(CASE WHEN closed_in_period = 1 AND stage_semantic_id = 'F' THEN 1 ELSE 0 END) AS failed_count,
                    SUM(CASE WHEN closed_in_period = 1 AND stage_semantic_id = 'F' THEN opportunity ELSE 0 END) AS failed_amount,
                    SUM(CASE WHEN closed_in_period = 1 AND stage_semantic_id IN ('S', 'F') THEN 1 ELSE 0 END) AS closed_count
                FROM sales_analytics_deals
                WHERE {where}
                """,
                (tenant_id, run_id),
            ).fetchone(),
            currency=self._main_currency(conn, tenant_id, run_id),
        )
        rows = conn.execute(
            f"""
            SELECT
                assigned_by_id AS manager_id,
                manager_name,
                COUNT(*) AS total_deals,
                SUM(opportunity) AS total_amount,
                SUM(CASE WHEN active_as_of_to = 1 THEN 1 ELSE 0 END) AS in_work_count,
                SUM(CASE WHEN active_as_of_to = 1 THEN opportunity ELSE 0 END) AS in_work_amount,
                SUM(CASE WHEN closed_in_period = 1 AND stage_semantic_id = 'S' THEN 1 ELSE 0 END) AS won_count,
                SUM(CASE WHEN closed_in_period = 1 AND stage_semantic_id = 'S' THEN opportunity ELSE 0 END) AS won_amount,
                SUM(CASE WHEN closed_in_period = 1 AND stage_semantic_id = 'F' THEN 1 ELSE 0 END) AS failed_count,
                SUM(CASE WHEN closed_in_period = 1 AND stage_semantic_id = 'F' THEN opportunity ELSE 0 END) AS failed_amount,
                SUM(CASE WHEN closed_in_period = 1 AND stage_semantic_id IN ('S', 'F') THEN 1 ELSE 0 END) AS closed_count
            FROM sales_analytics_deals
            WHERE {where}
            GROUP BY assigned_by_id, manager_name
            ORDER BY in_work_count DESC, total_amount DESC, manager_name ASC
            """,
            (tenant_id, run_id),
        ).fetchall()
        return {
            "source": "postgres:sales_analytics_deals",
            "department": department,
            "per_manager": [
                {
                    **_normalize_dashboard_row(row, currency=department["currency"]),
                    "manager_id": str(row["manager_id"] or ""),
                    "manager_name": row["manager_name"] or "",
                }
                for row in rows
            ],
        }

    def _task_status(self, conn: Any, tenant_id: str, run_id: str) -> dict[str, Any]:
        p = self._p
        rows = conn.execute(
            f"""
            WITH active_deals AS (
                SELECT id, assigned_by_id, manager_name
                FROM sales_analytics_deals
                WHERE tenant_id = {p}
                    AND run_id = {p}
                    AND active_as_of_to = 1
            ),
            unique_bindings AS (
                SELECT DISTINCT entity_id, task_id
                FROM sales_analytics_task_bindings
                WHERE tenant_id = {p}
                    AND run_id = {p}
                    AND entity_type = 'D'
            ),
            task_counts AS (
                SELECT
                    b.entity_id AS deal_id,
                    COUNT(*) AS total_tasks,
                    SUM(CASE WHEN t.is_completed = 0 THEN 1 ELSE 0 END) AS open_tasks,
                    SUM(CASE WHEN t.is_overdue = 1 THEN 1 ELSE 0 END) AS overdue_tasks
                FROM unique_bindings b
                INNER JOIN sales_analytics_tasks t
                    ON t.tenant_id = {p}
                    AND t.run_id = {p}
                    AND t.id = b.task_id
                GROUP BY b.entity_id
            ),
            per_deal AS (
                SELECT
                    d.id,
                    d.assigned_by_id,
                    d.manager_name,
                    COALESCE(t.total_tasks, 0) AS total_tasks,
                    COALESCE(t.open_tasks, 0) AS open_tasks,
                    COALESCE(t.overdue_tasks, 0) AS overdue_tasks
                FROM active_deals d
                LEFT JOIN task_counts t ON t.deal_id = d.id
            )
            SELECT
                0 AS is_manager,
                NULL AS manager_id,
                '' AS manager_name,
                COUNT(*) AS in_work_deals,
                SUM(CASE WHEN total_tasks > 0 THEN 1 ELSE 0 END) AS with_open_tasks,
                SUM(CASE WHEN total_tasks = 0 THEN 1 ELSE 0 END) AS without_open_tasks,
                SUM(CASE WHEN overdue_tasks > 0 THEN 1 ELSE 0 END) AS with_overdue_tasks,
                SUM(total_tasks) AS total_linked_tasks,
                SUM(open_tasks) AS open_linked_tasks,
                SUM(overdue_tasks) AS overdue_linked_tasks
            FROM per_deal
            UNION ALL
            SELECT
                1 AS is_manager,
                assigned_by_id AS manager_id,
                manager_name,
                COUNT(*) AS in_work_deals,
                SUM(CASE WHEN total_tasks > 0 THEN 1 ELSE 0 END) AS with_open_tasks,
                SUM(CASE WHEN total_tasks = 0 THEN 1 ELSE 0 END) AS without_open_tasks,
                SUM(CASE WHEN overdue_tasks > 0 THEN 1 ELSE 0 END) AS with_overdue_tasks,
                SUM(total_tasks) AS total_linked_tasks,
                SUM(open_tasks) AS open_linked_tasks,
                SUM(overdue_tasks) AS overdue_linked_tasks
            FROM per_deal
            GROUP BY assigned_by_id, manager_name
            ORDER BY is_manager ASC, in_work_deals DESC, manager_name ASC
            """,
            (tenant_id, run_id, tenant_id, run_id, tenant_id, run_id),
        ).fetchall()
        deal_rows = conn.execute(
            f"""
            WITH active_deals AS (
                SELECT id, assigned_by_id, manager_name
                FROM sales_analytics_deals
                WHERE tenant_id = {p}
                    AND run_id = {p}
                    AND active_as_of_to = 1
            ),
            unique_bindings AS (
                SELECT DISTINCT entity_id, task_id
                FROM sales_analytics_task_bindings
                WHERE tenant_id = {p}
                    AND run_id = {p}
                    AND entity_type = 'D'
            ),
            task_counts AS (
                SELECT
                    b.entity_id AS deal_id,
                    COUNT(*) AS total_tasks,
                    SUM(CASE WHEN t.is_completed = 0 THEN 1 ELSE 0 END) AS open_tasks,
                    SUM(CASE WHEN t.is_overdue = 1 THEN 1 ELSE 0 END) AS overdue_tasks
                FROM unique_bindings b
                INNER JOIN sales_analytics_tasks t
                    ON t.tenant_id = {p}
                    AND t.run_id = {p}
                    AND t.id = b.task_id
                GROUP BY b.entity_id
            )
            SELECT
                d.id AS deal_id,
                d.assigned_by_id AS manager_id,
                d.manager_name,
                COALESCE(t.total_tasks, 0) AS total_tasks,
                COALESCE(t.open_tasks, 0) AS open_tasks,
                COALESCE(t.overdue_tasks, 0) AS overdue_tasks
            FROM active_deals d
            LEFT JOIN task_counts t ON t.deal_id = d.id
            ORDER BY overdue_tasks DESC, open_tasks ASC, d.id ASC
            """,
            (tenant_id, run_id, tenant_id, run_id, tenant_id, run_id),
        ).fetchall()
        department = next((row for row in rows if _int(row["is_manager"]) == 0), None)
        managers = [row for row in rows if _int(row["is_manager"]) == 1]
        return {
            "source": "postgres:sales_analytics_tasks",
            "department": _normalize_task_row(department),
            "per_manager": [
                {
                    **_normalize_task_row(row),
                    "manager_id": str(row["manager_id"] or ""),
                    "manager_name": row["manager_name"] or "",
                }
                for row in managers
            ],
            "deals": [_normalize_task_deal_row(row) for row in deal_rows],
        }

    def _failure_reasons(self, conn: Any, tenant_id: str, run_id: str) -> dict[str, Any]:
        p = self._p
        params = (tenant_id, run_id)
        total_row = conn.execute(
            f"""
            SELECT COUNT(*) AS failed_deals_count, SUM(opportunity) AS failed_amount
            FROM sales_analytics_deals
            WHERE tenant_id = {p} AND run_id = {p}
                AND closed_in_period = 1 AND stage_semantic_id = 'F'
            """,
            params,
        ).fetchone()
        total = _int(total_row["failed_deals_count"] if total_row else 0)
        reasons = conn.execute(
            f"""
            SELECT
                COALESCE(NULLIF(stage_name, ''), NULLIF(stage_id, ''), 'Не указано') AS label,
                COALESCE(NULLIF(stage_id, ''), 'unknown') AS key,
                COUNT(*) AS count,
                SUM(opportunity) AS amount
            FROM sales_analytics_deals
            WHERE tenant_id = {p} AND run_id = {p}
                AND closed_in_period = 1 AND stage_semantic_id = 'F'
            GROUP BY label, key
            ORDER BY count DESC, amount DESC
            LIMIT 10
            """,
            params,
        ).fetchall()
        manager_totals = conn.execute(
            f"""
            SELECT
                assigned_by_id AS manager_id,
                manager_name,
                COUNT(*) AS failed_deals_count,
                SUM(opportunity) AS failed_amount
            FROM sales_analytics_deals
            WHERE tenant_id = {p} AND run_id = {p}
                AND closed_in_period = 1 AND stage_semantic_id = 'F'
            GROUP BY assigned_by_id, manager_name
            ORDER BY failed_deals_count DESC, failed_amount DESC, manager_name ASC
            """,
            params,
        ).fetchall()
        reason_rows = conn.execute(
            f"""
            SELECT
                assigned_by_id AS manager_id,
                COALESCE(NULLIF(stage_name, ''), NULLIF(stage_id, ''), 'Не указано') AS label,
                COALESCE(NULLIF(stage_id, ''), 'unknown') AS key,
                COUNT(*) AS count
            FROM sales_analytics_deals
            WHERE tenant_id = {p} AND run_id = {p}
                AND closed_in_period = 1 AND stage_semantic_id = 'F'
            GROUP BY assigned_by_id, label, key
            ORDER BY count DESC
            """,
            params,
        ).fetchall()
        by_manager_reasons: dict[str, list[dict[str, Any]]] = {}
        for row in reason_rows:
            manager_id = str(row["manager_id"] or "")
            by_manager_reasons.setdefault(manager_id, []).append(
                _reason_row(row, total=_manager_total(manager_totals, manager_id)),
            )
        return {
            "source": "postgres lost deals grouped by Bitrix lost stage",
            "failed_deals_count": total,
            "failed_amount": _num(total_row["failed_amount"] if total_row else 0),
            "failed_interactions_analyzed": total,
            "department_top_reasons": [_reason_row(row, total=total) for row in reasons],
            "per_manager": [
                {
                    "manager_id": str(row["manager_id"] or ""),
                    "manager_name": row["manager_name"] or "",
                    "failed_deals_count": _int(row["failed_deals_count"]),
                    "failed_amount": _num(row["failed_amount"]),
                    "failed_interactions_analyzed": _int(row["failed_deals_count"]),
                    "top_reasons": by_manager_reasons.get(str(row["manager_id"] or ""), [])[:5],
                }
                for row in manager_totals
            ],
        }

    def _lead_status(self, conn: Any, tenant_id: str, run_id: str) -> dict[str, Any]:
        p = self._p
        params = (tenant_id, run_id)
        totals = conn.execute(
            f"""
            SELECT
                COUNT(*) AS total_leads,
                SUM(CASE WHEN status_semantic_id = 'S' THEN 1 ELSE 0 END) AS converted_count,
                SUM(CASE WHEN status_semantic_id = 'F' THEN 1 ELSE 0 END) AS junk_count,
                SUM(CASE WHEN status_semantic_id NOT IN ('S', 'F') OR status_semantic_id IS NULL THEN 1 ELSE 0 END) AS in_work_count,
                SUM(opportunity) AS total_amount
            FROM sales_analytics_leads
            WHERE tenant_id = {p} AND run_id = {p}
            """,
            params,
        ).fetchone()
        total = _int(totals["total_leads"] if totals else 0)
        junk_total = _int(totals["junk_count"] if totals else 0)
        reasons = conn.execute(
            f"""
            SELECT
                COALESCE(NULLIF(status_name, ''), NULLIF(status_id, ''), 'not specified') AS label,
                COALESCE(NULLIF(status_id, ''), 'unknown') AS key,
                COUNT(*) AS count,
                SUM(opportunity) AS amount
            FROM sales_analytics_leads
            WHERE tenant_id = {p} AND run_id = {p}
                AND status_semantic_id = 'F'
            GROUP BY label, key
            ORDER BY count DESC, amount DESC
            LIMIT 10
            """,
            params,
        ).fetchall()
        managers = conn.execute(
            f"""
            SELECT
                assigned_by_id AS manager_id,
                manager_name,
                COUNT(*) AS total_leads,
                SUM(CASE WHEN status_semantic_id = 'S' THEN 1 ELSE 0 END) AS converted_count,
                SUM(CASE WHEN status_semantic_id = 'F' THEN 1 ELSE 0 END) AS junk_count,
                SUM(CASE WHEN status_semantic_id NOT IN ('S', 'F') OR status_semantic_id IS NULL THEN 1 ELSE 0 END) AS in_work_count,
                SUM(opportunity) AS total_amount
            FROM sales_analytics_leads
            WHERE tenant_id = {p} AND run_id = {p}
            GROUP BY assigned_by_id, manager_name
            ORDER BY total_leads DESC, manager_name ASC
            """,
            params,
        ).fetchall()
        manager_reasons = conn.execute(
            f"""
            SELECT
                assigned_by_id AS manager_id,
                COALESCE(NULLIF(status_name, ''), NULLIF(status_id, ''), 'not specified') AS label,
                COALESCE(NULLIF(status_id, ''), 'unknown') AS key,
                COUNT(*) AS count
            FROM sales_analytics_leads
            WHERE tenant_id = {p} AND run_id = {p}
                AND status_semantic_id = 'F'
            GROUP BY assigned_by_id, label, key
            ORDER BY count DESC
            """,
            params,
        ).fetchall()
        manager_totals = {
            str(row["manager_id"] or ""): _int(row["junk_count"])
            for row in managers
        }
        by_manager: dict[str, list[dict[str, Any]]] = {}
        for row in manager_reasons:
            manager_id = str(row["manager_id"] or "")
            by_manager.setdefault(manager_id, []).append(
                _reason_row(row, total=manager_totals.get(manager_id, 0)),
            )
        return {
            "source": "postgres:sales_analytics_leads",
            "department": {
                "total_leads": total,
                "converted_count": _int(totals["converted_count"] if totals else 0),
                "junk_count": junk_total,
                "in_work_count": _int(totals["in_work_count"] if totals else 0),
                "total_amount": round(_num(totals["total_amount"] if totals else 0), 2),
                "conversion_pct": _pct(_int(totals["converted_count"] if totals else 0), total),
                "junk_pct": _pct(junk_total, total),
            },
            "department_top_reasons": [_reason_row(row, total=junk_total) for row in reasons],
            "per_manager": [
                {
                    "manager_id": str(row["manager_id"] or ""),
                    "manager_name": row["manager_name"] or "",
                    "total_leads": _int(row["total_leads"]),
                    "converted_count": _int(row["converted_count"]),
                    "junk_count": _int(row["junk_count"]),
                    "in_work_count": _int(row["in_work_count"]),
                    "total_amount": round(_num(row["total_amount"]), 2),
                    "conversion_pct": _pct(_int(row["converted_count"]), _int(row["total_leads"])),
                    "junk_pct": _pct(_int(row["junk_count"]), _int(row["total_leads"])),
                    "top_reasons": by_manager.get(str(row["manager_id"] or ""), [])[:5],
                }
                for row in managers
            ],
        }

    def _revenue_summary(self, conn: Any, tenant_id: str, run_id: str) -> dict[str, Any]:
        p = self._p
        rows = conn.execute(
            f"""
            SELECT
                source,
                COUNT(*) AS document_count,
                SUM(amount) AS amount,
                SUM(CASE WHEN paid IN ('Y', '1', 'true', 'True', 'paid') THEN 1 ELSE 0 END) AS paid_count,
                SUM(CASE WHEN paid IN ('Y', '1', 'true', 'True', 'paid') THEN paid_amount ELSE 0 END) AS paid_amount
            FROM sales_analytics_revenue_documents
            WHERE tenant_id = {p} AND run_id = {p}
            GROUP BY source
            ORDER BY source ASC
            """,
            (tenant_id, run_id),
        ).fetchall()
        total_count = sum(_int(row["document_count"]) for row in rows)
        total_amount = sum(_num(row["amount"]) for row in rows)
        paid_count = sum(_int(row["paid_count"]) for row in rows)
        paid_amount = sum(_num(row["paid_amount"]) for row in rows)
        return {
            "source": "postgres:sales_analytics_revenue_documents",
            "department": {
                "document_count": total_count,
                "amount": round(total_amount, 2),
                "paid_count": paid_count,
                "paid_amount": round(paid_amount, 2),
                "paid_document_pct": _pct(paid_count, total_count),
                "currency": self._main_revenue_currency(conn, tenant_id, run_id),
            },
            "by_source": [
                {
                    "source": row["source"] or "",
                    "document_count": _int(row["document_count"]),
                    "amount": round(_num(row["amount"]), 2),
                    "paid_count": _int(row["paid_count"]),
                    "paid_amount": round(_num(row["paid_amount"]), 2),
                    "paid_document_pct": _pct(_int(row["paid_count"]), _int(row["document_count"])),
                }
                for row in rows
            ],
        }

    def _references(self, conn: Any, tenant_id: str, run_id: str) -> dict[str, Any]:
        p = self._p
        manager_rows = conn.execute(
            f"""
            SELECT DISTINCT assigned_by_id, manager_name
            FROM sales_analytics_deals
            WHERE tenant_id = {p} AND run_id = {p} AND assigned_by_id IS NOT NULL
            """,
            (tenant_id, run_id),
        ).fetchall()
        lead_manager_rows = conn.execute(
            f"""
            SELECT DISTINCT assigned_by_id, manager_name
            FROM sales_analytics_leads
            WHERE tenant_id = {p} AND run_id = {p} AND assigned_by_id IS NOT NULL
            """,
            (tenant_id, run_id),
        ).fetchall()
        stage_rows = conn.execute(
            f"""
            SELECT DISTINCT stage_id, stage_name
            FROM sales_analytics_deals
            WHERE tenant_id = {p} AND run_id = {p} AND stage_id IS NOT NULL
            """,
            (tenant_id, run_id),
        ).fetchall()
        lead_status_rows = conn.execute(
            f"""
            SELECT DISTINCT status_id, status_name
            FROM sales_analytics_leads
            WHERE tenant_id = {p} AND run_id = {p} AND status_id IS NOT NULL
            """,
            (tenant_id, run_id),
        ).fetchall()
        return {
            "manager_names": {
                **{str(row["assigned_by_id"]): row["manager_name"] or "" for row in manager_rows},
                **{str(row["assigned_by_id"]): row["manager_name"] or "" for row in lead_manager_rows},
            },
            "stage_names": {str(row["stage_id"]): row["stage_name"] or str(row["stage_id"]) for row in stage_rows},
            "lead_status_names": {
                str(row["status_id"]): row["status_name"] or str(row["status_id"])
                for row in lead_status_rows
            },
        }

    def _meta(self, conn: Any, tenant_id: str, run_id: str) -> dict[str, str]:
        p = self._p
        rows = conn.execute(
            f"""
            SELECT key, value
            FROM sales_analytics_run_meta
            WHERE tenant_id = {p} AND run_id = {p}
            """,
            (tenant_id, run_id),
        ).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def _main_currency(self, conn: Any, tenant_id: str, run_id: str) -> str:
        p = self._p
        row = conn.execute(
            f"""
            SELECT currency_id, COUNT(*) AS count
            FROM sales_analytics_deals
            WHERE tenant_id = {p} AND run_id = {p}
                AND currency_id IS NOT NULL AND currency_id != ''
            GROUP BY currency_id
            ORDER BY count DESC
            LIMIT 1
            """,
            (tenant_id, run_id),
        ).fetchone()
        return str(row["currency_id"] if row else "KZT")

    def _main_revenue_currency(self, conn: Any, tenant_id: str, run_id: str) -> str:
        p = self._p
        row = conn.execute(
            f"""
            SELECT currency_id, COUNT(*) AS count
            FROM sales_analytics_revenue_documents
            WHERE tenant_id = {p} AND run_id = {p}
                AND currency_id IS NOT NULL AND currency_id != ''
            GROUP BY currency_id
            ORDER BY count DESC
            LIMIT 1
            """,
            (tenant_id, run_id),
        ).fetchone()
        return str(row["currency_id"] if row else "KZT")


def _normalize_dashboard_row(row: Any, *, currency: str) -> dict[str, Any]:
    closed = _int(row["closed_count"] if row else 0)
    won = _int(row["won_count"] if row else 0)
    return {
        "total_deals": _int(row["total_deals"] if row else 0),
        "total_amount": round(_num(row["total_amount"] if row else 0), 2),
        "in_work_count": _int(row["in_work_count"] if row else 0),
        "in_work_amount": round(_num(row["in_work_amount"] if row else 0), 2),
        "won_count": won,
        "won_amount": round(_num(row["won_amount"] if row else 0), 2),
        "failed_count": _int(row["failed_count"] if row else 0),
        "failed_amount": round(_num(row["failed_amount"] if row else 0), 2),
        "closed_count": closed,
        "win_rate_closed_pct": _pct(won, closed),
        "currency": currency,
    }


def _normalize_task_row(row: Any) -> dict[str, Any]:
    total = _int(row["in_work_deals"] if row else 0)
    without = _int(row["without_open_tasks"] if row else 0)
    overdue = _int(row["with_overdue_tasks"] if row else 0)
    return {
        "in_work_deals": total,
        "with_open_tasks": _int(row["with_open_tasks"] if row else 0),
        "without_open_tasks": without,
        "with_overdue_tasks": overdue,
        "without_open_tasks_pct": _pct(without, total),
        "with_overdue_tasks_pct": _pct(overdue, total),
        "total_linked_tasks": _int(row["total_linked_tasks"] if row else 0),
        "open_linked_tasks": _int(row["open_linked_tasks"] if row else 0),
        "overdue_linked_tasks": _int(row["overdue_linked_tasks"] if row else 0),
    }


def _normalize_task_deal_row(row: Any) -> dict[str, Any]:
    open_tasks = _int(row["open_tasks"] if row else 0)
    overdue_tasks = _int(row["overdue_tasks"] if row else 0)
    total_tasks = _int(row["total_tasks"] if row else 0)
    return {
        "deal_id": str(row["deal_id"] if row else ""),
        "manager_id": str(row["manager_id"] if row else ""),
        "manager_name": str(row["manager_name"] if row else ""),
        "total_task_count": total_tasks,
        "active_task_count": open_tasks,
        "overdue_task_count": overdue_tasks,
        "has_active_task": open_tasks > 0,
        "has_overdue_task": overdue_tasks > 0,
    }


def _reason_row(row: Any, *, total: int) -> dict[str, Any]:
    count = _int(row["count"])
    try:
        amount = row["amount"]
    except (KeyError, IndexError):
        amount = 0
    return {
        "key": str(row["key"] or ""),
        "label": str(row["label"] or "Не указано"),
        "count": count,
        "total": total,
        "pct": _pct(count, total),
        "amount": _num(amount),
    }


def _manager_total(rows: list[Any], manager_id: str) -> int:
    for row in rows:
        if str(row["manager_id"] or "") == str(manager_id):
            return _int(row["failed_deals_count"])
    return 0


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _pct(count: int | float, total: int | float) -> float:
    c = _num(count)
    t = _num(total)
    return round(c / t * 100, 1) if t else 0.0
