from __future__ import annotations

from typing import Any

from bitrix_ingest.application.catalog import GetCatalogService
from bitrix_ingest.application.date_range import within_datetime_range


class FakeGateway:
    def __init__(
        self,
        *,
        calls: dict[str, dict[str, Any]] | None = None,
        deals: list[dict[str, Any]] | None = None,
    ) -> None:
        self._calls = calls or {}
        self._deals = deals or []
        self.item_requests: list[dict[str, Any]] = []
        self.user_requests: list[dict[str, Any]] = []

    def call(
        self,
        method: str,
        body: dict[str, Any] | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        if method == "crm.item.list":
            payload = body or {}
            self.item_requests.append(payload)
            rows = [self._deal_to_item(deal) for deal in self._deals]
            rows = [
                row for row in rows
                if self._matches_filter(row, payload.get("filter") or {})
            ]
            rows = self._sort_rows(rows, payload.get("order") or {})
            start = int(payload.get("start") or 0)
            page = rows[start:start + 50]
            response: dict[str, Any] = {"result": {"items": page}}
            if start + 50 < len(rows):
                response["next"] = start + 50
            return response
        if method == "user.get":
            payload = body or {}
            self.user_requests.append(payload)
            response = self._calls[method]
            rows = response.get("result") or []
            if not isinstance(rows, list):
                return response
            if "ID" in payload:
                response = self._calls.get("user.get.by_id", response)
                rows = response.get("result") or []
                if not isinstance(rows, list):
                    return response
                wanted_id = str(payload.get("ID") or "")
                return {
                    "result": [
                        row for row in rows
                        if str(row.get("ID") or "") == wanted_id
                    ]
                }
            start = int(payload.get("start") or 0)
            page = rows[start:start + 50]
            paginated: dict[str, Any] = {"result": page}
            if start + 50 < len(rows):
                paginated["next"] = start + 50
            return paginated
        return self._calls[method]

    def list_all(
        self,
        method: str,
        select: list[str],
        filter: dict[str, Any] | None = None,
        order: dict[str, Any] | None = None,
        context: str = "",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        raise AssertionError("audit preview must not use crm.deal.list/list_all anymore")

    @staticmethod
    def _deal_to_item(deal: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(str(deal.get("ID") or "0")),
            "title": str(deal.get("TITLE") or ""),
            "sourceId": str(deal.get("SOURCE_ID") or ""),
            "assignedById": int(str(deal.get("ASSIGNED_BY_ID") or "0")),
            "categoryId": int(str(deal.get("CATEGORY_ID") or "0")),
            "createdTime": str(deal.get("DATE_CREATE") or ""),
            "updatedTime": str(deal.get("DATE_MODIFY") or ""),
        }

    @staticmethod
    def _matches_filter(item: dict[str, Any], filter_: dict[str, Any]) -> bool:
        logic = str(filter_.get("logic") or "").lower()
        direct_conditions: list[tuple[str, Any]] = []
        groups: list[dict[str, Any]] = []

        for raw_key, value in filter_.items():
            if raw_key == "logic":
                continue
            if isinstance(raw_key, int) or (isinstance(raw_key, str) and raw_key.isdigit()):
                if isinstance(value, dict):
                    groups.append(value)
                continue
            direct_conditions.append((str(raw_key), value))

        if not all(FakeGateway._matches_condition(item, key, value) for key, value in direct_conditions):
            return False
        if not groups:
            return True
        if logic == "or":
            return any(FakeGateway._matches_filter(item, group) for group in groups)
        return all(FakeGateway._matches_filter(item, group) for group in groups)

    @staticmethod
    def _matches_condition(item: dict[str, Any], key: str, value: Any) -> bool:
        if key.startswith(">="):
            field = key[2:]
            return within_datetime_range(
                str(item.get(field) or ""),
                date_from=str(value),
                date_to=None,
            )
        if key.startswith("<="):
            field = key[2:]
            return within_datetime_range(
                str(item.get(field) or ""),
                date_from=None,
                date_to=str(value),
            )
        if isinstance(value, list):
            return str(item.get(key) or "") in {str(v) for v in value}
        return str(item.get(key) or "") == str(value)

    @staticmethod
    def _sort_rows(rows: list[dict[str, Any]], order: dict[str, Any]) -> list[dict[str, Any]]:
        sorted_rows = list(rows)
        for field, direction in reversed(list(order.items())):
            reverse = str(direction).upper() == "DESC"
            sorted_rows.sort(
                key=lambda row: FakeGateway._sort_key(row.get(field)),
                reverse=reverse,
            )
        return sorted_rows

    @staticmethod
    def _sort_key(value: Any) -> Any:
        if isinstance(value, (int, float)):
            return value
        text = str(value or "")
        return int(text) if text.isdigit() else text


def _service_with_deals(deals: list[dict[str, Any]]) -> tuple[GetCatalogService, FakeGateway]:
    users = [
        {"ID": "20", "NAME": "Aset", "LAST_NAME": "SP", "EMAIL": "20@example.com", "ACTIVE": True},
        {"ID": "30", "NAME": "Raushan", "LAST_NAME": "SP", "EMAIL": "30@example.com", "ACTIVE": True},
        {"ID": "99", "NAME": "Ardak", "LAST_NAME": "SP", "EMAIL": "99@example.com", "ACTIVE": True},
    ]
    funnels = [
        {"id": 0, "name": "Default", "sort": 0, "entityTypeId": 2, "isDefault": "Y"},
        {"id": 4, "name": "Doors", "sort": 20, "entityTypeId": 2, "isDefault": "N"},
    ]
    return _service_with_custom_catalog(deals, users=users, funnels=funnels)


def _service_with_custom_catalog(
    deals: list[dict[str, Any]],
    *,
    users: list[dict[str, Any]],
    funnels: list[dict[str, Any]],
) -> tuple[GetCatalogService, FakeGateway]:
    gateway = FakeGateway(
        deals=deals,
        calls={
            "user.get": {"result": users},
            "crm.category.list": {"result": {"categories": funnels}},
        },
    )
    return GetCatalogService(gateway=gateway), gateway


def test_get_funnels_returns_default_and_named_funnels() -> None:
    service, _gateway = _service_with_deals([])

    funnels = service.get_funnels()

    assert funnels == [
        {"id": "0", "name": "Default", "sort": 0},
        {"id": "4", "name": "Doors", "sort": 20},
    ]


def test_get_funnels_with_managers_groups_managers_by_funnel() -> None:
    service, _gateway = _service_with_deals([
        {
            "ID": "1",
            "TITLE": "Default funnel lead",
            "SOURCE_ID": "CALL",
            "ASSIGNED_BY_ID": "20",
            "CATEGORY_ID": "0",
            "DATE_CREATE": "2026-02-01T10:00:00+03:00",
            "DATE_MODIFY": "2026-02-01T10:00:00+03:00",
        },
        {
            "ID": "2",
            "TITLE": "Doors lead 1",
            "SOURCE_ID": "CALL",
            "ASSIGNED_BY_ID": "30",
            "CATEGORY_ID": "4",
            "DATE_CREATE": "2026-02-02T10:00:00+03:00",
            "DATE_MODIFY": "2026-02-02T10:00:00+03:00",
        },
        {
            "ID": "3",
            "TITLE": "Doors lead 2",
            "SOURCE_ID": "WZ001",
            "ASSIGNED_BY_ID": "20",
            "CATEGORY_ID": "4",
            "DATE_CREATE": "2026-02-03T10:00:00+03:00",
            "DATE_MODIFY": "2026-02-03T10:00:00+03:00",
        },
    ])

    funnels = service.get_funnels_with_managers()

    assert funnels == [
        {
            "id": "0",
            "name": "Default",
            "sort": 0,
            "manager_count": 1,
            "managers": [
                {"id": "20", "name": "Aset SP", "email": "20@example.com", "active": True},
            ],
        },
        {
            "id": "4",
            "name": "Doors",
            "sort": 20,
            "manager_count": 2,
            "managers": [
                {"id": "20", "name": "Aset SP", "email": "20@example.com", "active": True},
                {"id": "30", "name": "Raushan SP", "email": "30@example.com", "active": True},
            ],
        },
    ]


def test_get_funnels_with_managers_can_filter_inactive_users() -> None:
    service, _gateway = _service_with_custom_catalog(
        [
            {
                "ID": "1",
                "TITLE": "Doors lead 1",
                "SOURCE_ID": "CALL",
                "ASSIGNED_BY_ID": "20",
                "CATEGORY_ID": "4",
                "DATE_CREATE": "2026-02-02T10:00:00+03:00",
                "DATE_MODIFY": "2026-02-02T10:00:00+03:00",
            },
            {
                "ID": "2",
                "TITLE": "Doors lead 2",
                "SOURCE_ID": "CALL",
                "ASSIGNED_BY_ID": "99",
                "CATEGORY_ID": "4",
                "DATE_CREATE": "2026-02-03T10:00:00+03:00",
                "DATE_MODIFY": "2026-02-03T10:00:00+03:00",
            },
        ],
        users=[
            {"ID": "20", "NAME": "Aset", "LAST_NAME": "SP", "EMAIL": "20@example.com", "ACTIVE": True},
            {"ID": "99", "NAME": "Dormant", "LAST_NAME": "SP", "EMAIL": "99@example.com", "ACTIVE": False},
        ],
        funnels=[
            {"id": 4, "name": "Doors", "sort": 20, "entityTypeId": 2, "isDefault": "N"},
        ],
    )

    funnels = service.get_funnels_with_managers(active_only=True)

    assert funnels == [
        {
            "id": "4",
            "name": "Doors",
            "sort": 20,
            "manager_count": 1,
            "managers": [
                {"id": "20", "name": "Aset SP", "email": "20@example.com", "active": True},
            ],
        },
    ]


def test_preview_returns_scope_managers_without_responsible_filter() -> None:
    service, _gateway = _service_with_deals([
        {
            "ID": "1",
            "TITLE": "WhatsApp lead 1",
            "SOURCE_ID": "WZ001",
            "ASSIGNED_BY_ID": "20",
            "CATEGORY_ID": "4",
            "DATE_CREATE": "2026-02-01T10:00:00+03:00",
            "DATE_MODIFY": "2026-02-01T10:00:00+03:00",
        },
        {
            "ID": "2",
            "TITLE": "WhatsApp lead 2",
            "SOURCE_ID": "WZ002",
            "ASSIGNED_BY_ID": "30",
            "CATEGORY_ID": "4",
            "DATE_CREATE": "2026-02-03T10:00:00+03:00",
            "DATE_MODIFY": "2026-02-03T10:00:00+03:00",
        },
    ])

    preview = service.get_audit_preview(
        funnel_ids=["4"],
        date_from="2026-01-01",
        date_to="2026-03-01",
    )

    assert preview["deal_count"] == 2
    assert preview["manager_count"] == 2
    assert preview["scope_manager_count"] == 2
    assert [manager["id"] for manager in preview["scope_managers"]] == ["20", "30"]
    assert preview["warnings"] == []
    assert preview["responsible_filter_mode"] == "deal_owner"


def test_preview_warns_when_responsible_is_not_in_scope() -> None:
    service, _gateway = _service_with_deals([
        {
            "ID": "1",
            "TITLE": "WhatsApp lead 1",
            "SOURCE_ID": "WZ001",
            "ASSIGNED_BY_ID": "20",
            "CATEGORY_ID": "4",
            "DATE_CREATE": "2026-02-01T10:00:00+03:00",
            "DATE_MODIFY": "2026-02-01T10:00:00+03:00",
        },
        {
            "ID": "2",
            "TITLE": "WhatsApp lead 2",
            "SOURCE_ID": "WZ002",
            "ASSIGNED_BY_ID": "30",
            "CATEGORY_ID": "4",
            "DATE_CREATE": "2026-02-03T10:00:00+03:00",
            "DATE_MODIFY": "2026-02-03T10:00:00+03:00",
        },
    ])

    preview = service.get_audit_preview(
        funnel_ids=["4"],
        date_from="2026-01-01",
        date_to="2026-03-01",
        responsible_id="99",
    )

    assert preview["deal_count"] == 0
    assert preview["manager_count"] == 0
    assert preview["scope_manager_count"] == 2
    assert preview["total_deals_scanned"] == 0
    assert preview["warnings"][0]["code"] == "responsible_not_in_scope"
    assert [manager["id"] for manager in preview["warnings"][0]["available_scope_managers"]] == ["20", "30"]


def test_preview_warns_when_responsible_has_no_whatsapp_deals() -> None:
    service, _gateway = _service_with_deals([
        {
            "ID": "1",
            "TITLE": "Regular lead",
            "SOURCE_ID": "CALL",
            "ASSIGNED_BY_ID": "99",
            "CATEGORY_ID": "4",
            "DATE_CREATE": "2026-02-01T10:00:00+03:00",
            "DATE_MODIFY": "2026-02-01T10:00:00+03:00",
        },
        {
            "ID": "2",
            "TITLE": "WhatsApp lead 2",
            "SOURCE_ID": "WZ002",
            "ASSIGNED_BY_ID": "20",
            "CATEGORY_ID": "4",
            "DATE_CREATE": "2026-02-03T10:00:00+03:00",
            "DATE_MODIFY": "2026-02-03T10:00:00+03:00",
        },
    ])

    preview = service.get_audit_preview(
        funnel_ids=["4"],
        date_from="2026-01-01",
        date_to="2026-03-01",
        responsible_id="99",
    )

    assert preview["deal_count"] == 0
    assert preview["manager_count"] == 0
    assert preview["scope_manager_count"] == 1
    assert preview["total_deals_scanned"] == 1
    assert preview["warnings"][0]["code"] == "responsible_has_no_whatsapp_deals"


def test_preview_includes_deal_when_create_date_is_in_scope() -> None:
    service, gateway = _service_with_deals([
        {
            "ID": "1",
            "TITLE": "WhatsApp lead 1",
            "SOURCE_ID": "WZ001",
            "ASSIGNED_BY_ID": "20",
            "CATEGORY_ID": "4",
            "DATE_CREATE": "2026-02-01T10:00:00+03:00",
            "DATE_MODIFY": "2025-12-15T10:00:00+03:00",
        },
    ])

    preview = service.get_audit_preview(
        funnel_ids=["4"],
        date_from="2026-01-01",
        date_to="2026-03-01",
    )

    assert preview["deal_count"] == 1
    assert preview["manager_count"] == 1
    assert preview["actual_date_from"] == "2026-02-01T10:00:00+03:00"
    assert preview["actual_date_to"] == "2026-02-01T10:00:00+03:00"
    assert len(gateway.item_requests) == 1
    assert gateway.item_requests[0]["filter"] == {
        "categoryId": 4,
        0: {
            "logic": "OR",
            0: {
                ">=createdTime": "2026-01-01T00:00:00",
                "<=createdTime": "2026-03-01T23:59:59",
            },
            1: {
                ">=updatedTime": "2026-01-01T00:00:00",
                "<=updatedTime": "2026-03-01T23:59:59",
            },
        },
    }


def test_preview_loads_all_item_pages() -> None:
    deals = [
        {
            "ID": str(index),
            "TITLE": f"Lead {index} - WhatsApp",
            "SOURCE_ID": "WZ001",
            "ASSIGNED_BY_ID": "20",
            "CATEGORY_ID": "4",
            "DATE_CREATE": "2026-02-01T10:00:00+03:00",
            "DATE_MODIFY": "2026-02-01T10:00:00+03:00",
        }
        for index in range(1, 56)
    ]
    service, gateway = _service_with_deals(deals)

    preview = service.get_audit_preview(
        funnel_ids=["4"],
        date_from="2026-01-01",
        date_to="2026-03-01",
    )

    assert preview["deal_count"] == 55
    assert preview["total_deals_scanned"] == 55
    assert [request["start"] for request in gateway.item_requests] == [0, 50]


def test_get_funnels_with_managers_loads_all_item_pages() -> None:
    deals = [
        {
            "ID": str(index),
            "TITLE": f"Lead {index}",
            "SOURCE_ID": "CALL",
            "ASSIGNED_BY_ID": "20" if index % 2 else "30",
            "CATEGORY_ID": "4",
            "DATE_CREATE": "2026-02-01T10:00:00+03:00",
            "DATE_MODIFY": "2026-02-01T10:00:00+03:00",
        }
        for index in range(1, 56)
    ]
    service, gateway = _service_with_deals(deals)

    funnels = service.get_funnels_with_managers()

    assert funnels == [
        {
            "id": "0",
            "name": "Default",
            "sort": 0,
            "manager_count": 0,
            "managers": [],
        },
        {
            "id": "4",
            "name": "Doors",
            "sort": 20,
            "manager_count": 2,
            "managers": [
                {"id": "20", "name": "Aset SP", "email": "20@example.com", "active": True},
                {"id": "30", "name": "Raushan SP", "email": "30@example.com", "active": True},
            ],
        },
    ]
    assert [request["start"] for request in gateway.item_requests] == [0, 50]


def test_get_funnels_with_managers_resolves_users_from_later_pages() -> None:
    users = [
        {
            "ID": str(index),
            "NAME": f"User{index}",
            "LAST_NAME": "SP",
            "EMAIL": f"{index}@example.com",
            "ACTIVE": True,
        }
        for index in range(1, 55)
    ]
    users.append({
        "ID": "70",
        "NAME": "Aziza",
        "LAST_NAME": "Kurbanbay",
        "SECOND_NAME": "Zairkyzy",
        "EMAIL": "70@example.com",
        "ACTIVE": True,
    })
    service, gateway = _service_with_custom_catalog(
        [
            {
                "ID": "1",
                "TITLE": "Later page manager",
                "SOURCE_ID": "CALL",
                "ASSIGNED_BY_ID": "70",
                "CATEGORY_ID": "4",
                "DATE_CREATE": "2026-02-01T10:00:00+03:00",
                "DATE_MODIFY": "2026-02-01T10:00:00+03:00",
            },
        ],
        users=users,
        funnels=[
            {"id": 4, "name": "Doors", "sort": 20, "entityTypeId": 2, "isDefault": "N"},
        ],
    )

    funnels = service.get_funnels_with_managers()

    assert funnels[0]["managers"] == [
        {
            "id": "70",
            "name": "Aziza Kurbanbay Zairkyzy",
            "email": "70@example.com",
            "active": True,
        },
    ]
    assert [request["start"] for request in gateway.user_requests] == [0, 50]


def test_audit_preview_resolves_missing_manager_by_direct_user_lookup() -> None:
    gateway = FakeGateway(
        deals=[
            {
                "ID": "1",
                "TITLE": "Manager missing from user list",
                "SOURCE_ID": "WZ001",
                "ASSIGNED_BY_ID": "5",
                "CATEGORY_ID": "4",
                "DATE_CREATE": "2026-02-01T10:00:00+03:00",
                "DATE_MODIFY": "2026-02-01T10:00:00+03:00",
            },
        ],
        calls={
            "user.get": {
                "result": [
                    {"ID": "20", "NAME": "Aset", "LAST_NAME": "SP", "EMAIL": "20@example.com", "ACTIVE": True},
                ],
            },
            "user.get.by_id": {
                "result": [
                    {
                        "ID": "5",
                        "NAME": "Alla",
                        "LAST_NAME": "Shevchenko",
                        "EMAIL": "5@example.com",
                        "ACTIVE": True,
                    },
                ],
            },
            "crm.category.list": {
                "result": {
                    "categories": [
                        {"id": 4, "name": "Doors", "sort": 20, "entityTypeId": 2, "isDefault": "N"},
                    ],
                },
            },
        },
    )
    service = GetCatalogService(gateway=gateway)

    preview = service.get_audit_preview(funnel_ids=["4"])

    assert preview["scope_managers"] == [
        {
            "id": "5",
            "name": "Alla Shevchenko",
            "email": "5@example.com",
            "active": True,
        },
    ]
    assert {"ID": 5} in gateway.user_requests
