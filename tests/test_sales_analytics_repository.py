from __future__ import annotations

import sqlite3

from bitrix_ingest.infrastructure.database.sales_analytics_repository import (
    SalesAnalyticsRepository,
)


def _repo_for_sqlite() -> SalesAnalyticsRepository:
    class FakeDb:
        placeholder = "?"

    repo = SalesAnalyticsRepository.__new__(SalesAnalyticsRepository)
    repo._db = FakeDb()
    return repo


def test_task_status_counts_active_deals_without_duplicate_task_bindings() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sales_analytics_deals (
            tenant_id TEXT,
            run_id TEXT,
            id INTEGER,
            assigned_by_id INTEGER,
            manager_name TEXT,
            active_as_of_to INTEGER
        );
        CREATE TABLE sales_analytics_task_bindings (
            tenant_id TEXT,
            run_id TEXT,
            task_id INTEGER,
            entity_type TEXT,
            entity_id INTEGER
        );
        CREATE TABLE sales_analytics_tasks (
            tenant_id TEXT,
            run_id TEXT,
            id INTEGER,
            is_completed INTEGER,
            is_overdue INTEGER
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO sales_analytics_deals
            (tenant_id, run_id, id, assigned_by_id, manager_name, active_as_of_to)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("tenant", "run", 1, 10, "Alice", 1),
            ("tenant", "run", 2, 10, "Alice", 1),
            ("tenant", "run", 3, 20, "Bob", 1),
            ("tenant", "run", 4, 20, "Bob", 0),
        ],
    )
    conn.executemany(
        """
        INSERT INTO sales_analytics_tasks
            (tenant_id, run_id, id, is_completed, is_overdue)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("tenant", "run", 100, 0, 0),
            ("tenant", "run", 101, 1, 1),
            ("tenant", "run", 102, 0, 1),
            ("tenant", "run", 103, 0, 1),
        ],
    )
    conn.executemany(
        """
        INSERT INTO sales_analytics_task_bindings
            (tenant_id, run_id, task_id, entity_type, entity_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("tenant", "run", 100, "D", 1),
            ("tenant", "run", 100, "D", 1),
            ("tenant", "run", 101, "D", 1),
            ("tenant", "run", 102, "D", 3),
            ("tenant", "run", 103, "D", 4),
        ],
    )

    result = _repo_for_sqlite()._task_status(conn, "tenant", "run")

    assert result["department"] == {
        "in_work_deals": 3,
        "with_open_tasks": 2,
        "without_open_tasks": 1,
        "with_overdue_tasks": 2,
        "without_open_tasks_pct": 33.3,
        "with_overdue_tasks_pct": 66.7,
        "total_linked_tasks": 3,
        "open_linked_tasks": 2,
        "overdue_linked_tasks": 2,
    }
    assert result["per_manager"] == [
        {
            "in_work_deals": 2,
            "with_open_tasks": 1,
            "without_open_tasks": 1,
            "with_overdue_tasks": 1,
            "without_open_tasks_pct": 50.0,
            "with_overdue_tasks_pct": 50.0,
            "total_linked_tasks": 2,
            "open_linked_tasks": 1,
            "overdue_linked_tasks": 1,
            "manager_id": "10",
            "manager_name": "Alice",
        },
        {
            "in_work_deals": 1,
            "with_open_tasks": 1,
            "without_open_tasks": 0,
            "with_overdue_tasks": 1,
            "without_open_tasks_pct": 0.0,
            "with_overdue_tasks_pct": 100.0,
            "total_linked_tasks": 1,
            "open_linked_tasks": 1,
            "overdue_linked_tasks": 1,
            "manager_id": "20",
            "manager_name": "Bob",
        },
    ]
