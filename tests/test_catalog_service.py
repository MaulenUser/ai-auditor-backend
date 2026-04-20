from __future__ import annotations

from typing import Any

from bitrix_ingest.application.catalog import GetCatalogService


class FakeGateway:
    def __init__(
        self,
        *,
        calls: dict[str, dict[str, Any]] | None = None,
        deals: list[dict[str, Any]] | None = None,
    ) -> None:
        self._calls = calls or {}
        self._deals = deals or []

    def call(
        self,
        method: str,
        body: dict[str, Any] | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
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
        assert method == "crm.deal.list"
        return list(self._deals)


def _service_with_deals(deals: list[dict[str, Any]]) -> GetCatalogService:
    return GetCatalogService(
        gateway=FakeGateway(
            deals=deals,
            calls={
                "user.get": {
                    "result": [
                        {"ID": "20", "NAME": "Aset", "LAST_NAME": "SP", "EMAIL": "20@example.com", "ACTIVE": True},
                        {"ID": "30", "NAME": "Raushan", "LAST_NAME": "SP", "EMAIL": "30@example.com", "ACTIVE": True},
                        {"ID": "99", "NAME": "Ardak", "LAST_NAME": "SP", "EMAIL": "99@example.com", "ACTIVE": True},
                    ]
                },
                "crm.dealcategory.list": {
                    "result": [
                        {"ID": "4", "NAME": "Doors", "SORT": 20},
                    ]
                },
            },
        )
    )


def test_preview_returns_scope_managers_without_responsible_filter() -> None:
    service = _service_with_deals([
        {
            "ID": "1",
            "TITLE": "WhatsApp lead 1",
            "SOURCE_ID": "WZ001",
            "ASSIGNED_BY_ID": "20",
            "CATEGORY_ID": "4",
            "DATE_MODIFY": "2026-02-01T10:00:00+03:00",
        },
        {
            "ID": "2",
            "TITLE": "WhatsApp lead 2",
            "SOURCE_ID": "WZ002",
            "ASSIGNED_BY_ID": "30",
            "CATEGORY_ID": "4",
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
    service = _service_with_deals([
        {
            "ID": "1",
            "TITLE": "WhatsApp lead 1",
            "SOURCE_ID": "WZ001",
            "ASSIGNED_BY_ID": "20",
            "CATEGORY_ID": "4",
            "DATE_MODIFY": "2026-02-01T10:00:00+03:00",
        },
        {
            "ID": "2",
            "TITLE": "WhatsApp lead 2",
            "SOURCE_ID": "WZ002",
            "ASSIGNED_BY_ID": "30",
            "CATEGORY_ID": "4",
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
    service = _service_with_deals([
        {
            "ID": "1",
            "TITLE": "Regular lead",
            "SOURCE_ID": "CALL",
            "ASSIGNED_BY_ID": "99",
            "CATEGORY_ID": "4",
            "DATE_MODIFY": "2026-02-01T10:00:00+03:00",
        },
        {
            "ID": "2",
            "TITLE": "WhatsApp lead 2",
            "SOURCE_ID": "WZ002",
            "ASSIGNED_BY_ID": "20",
            "CATEGORY_ID": "4",
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
