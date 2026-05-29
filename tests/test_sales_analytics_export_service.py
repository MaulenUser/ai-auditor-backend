from __future__ import annotations

from typing import Any

from bitrix_ingest.application.sales_analytics import (
    ExportSalesAnalyticsRequest,
    ExportSalesAnalyticsService,
)


class CapturingGateway:
    def __init__(self) -> None:
        self.list_all_calls: list[dict[str, Any]] = []

    def list_all(
        self,
        method: str,
        *,
        select: list[str],
        filter: dict[str, Any],
        order: dict[str, Any],
        context: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self.list_all_calls.append(
            {
                "method": method,
                "select": select,
                "filter": filter,
                "order": order,
                "context": context,
                "limit": limit,
            }
        )
        if method != "crm.deal.list":
            raise AssertionError(f"Unexpected list_all: {method}")
        return [
            {
                "ID": "101",
                "TITLE": "Created open",
                "STAGE_ID": "NEW",
                "STAGE_SEMANTIC_ID": "P",
                "CATEGORY_ID": "0",
                "ASSIGNED_BY_ID": "7",
                "OPPORTUNITY": "100",
                "CURRENCY_ID": "KZT",
                "DATE_CREATE": "2026-05-01T10:00:00+03:00",
                "DATE_MODIFY": "2026-05-02T10:00:00+03:00",
                "CLOSED": "N",
            },
            {
                "ID": "102",
                "TITLE": "Created lost",
                "STAGE_ID": "LOSE",
                "STAGE_SEMANTIC_ID": "F",
                "CATEGORY_ID": "0",
                "ASSIGNED_BY_ID": "8",
                "OPPORTUNITY": "200",
                "CURRENCY_ID": "KZT",
                "DATE_CREATE": "2026-05-03T10:00:00+03:00",
                "DATE_MODIFY": "2026-05-04T10:00:00+03:00",
                "CLOSEDATE": "2026-05-05T10:00:00+03:00",
                "CLOSED": "Y",
            },
        ]

    def call(self, method: str, body: dict[str, Any] | None = None, label: str | None = None):
        if method == "user.get":
            return {
                "result": [
                    {"ID": "7", "NAME": "Open", "LAST_NAME": "Manager"},
                    {"ID": "8", "NAME": "Lost", "LAST_NAME": "Manager"},
                ]
            }
        if method == "department.get":
            return {"result": []}
        if method == "crm.dealcategory.list":
            return {"result": []}
        if method == "crm.status.list":
            return {
                "result": [
                    {"STATUS_ID": "NEW", "NAME": "New"},
                    {"STATUS_ID": "LOSE", "NAME": "Lost"},
                ]
            }
        raise AssertionError(f"Unexpected call: {method}")


class CapturingRepository:
    def __init__(self) -> None:
        self.meta: dict[str, Any] | None = None
        self.deals: list[dict[str, Any]] | None = None

    def replace_snapshot(
        self,
        *,
        tenant_id: str,
        run_id: str,
        meta: dict[str, Any],
        deals: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
        task_bindings: list[dict[str, Any]],
        leads: list[dict[str, Any]],
        revenue_documents: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.meta = meta
        self.deals = deals
        return {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "meta": meta,
            "deal_dashboard": {},
        }


def test_sales_analytics_deals_use_date_create_scope_only() -> None:
    gateway = CapturingGateway()
    repository = CapturingRepository()

    ExportSalesAnalyticsService(gateway=gateway, repository=repository).execute(
        ExportSalesAnalyticsRequest(
            tenant_id="client_A12",
            run_id="run_1",
            date_from="2026-04-29",
            date_to="2026-05-29",
            category_ids=["0"],
            include_tasks=False,
            include_leads=False,
            include_revenue=False,
        )
    )

    deal_calls = [call for call in gateway.list_all_calls if call["method"] == "crm.deal.list"]
    assert len(deal_calls) == 1
    assert deal_calls[0]["filter"] == {
        "CATEGORY_ID": "0",
        ">=DATE_CREATE": "2026-04-29T00:00:00",
        "<=DATE_CREATE": "2026-05-29T23:59:59",
    }

    assert repository.meta is not None
    assert repository.meta["deal_date_filter"] == "DATE_CREATE"
    assert repository.meta["deals_unique"] == 2
    assert repository.meta["created_in_period"] == 2
    assert repository.meta["active_as_of_to"] == 1
    assert repository.meta["closed_in_period"] == 1
    assert repository.meta["modified_in_period"] == 2

    assert repository.deals is not None
    by_id = {row["id"]: row for row in repository.deals}
    assert by_id[101]["active_as_of_to"] == 1
    assert by_id[101]["closed_in_period"] == 0
    assert by_id[102]["active_as_of_to"] == 0
    assert by_id[102]["closed_in_period"] == 1
