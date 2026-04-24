"""Tests for shared date-range filtering across exporters."""
from __future__ import annotations

from unittest.mock import MagicMock

from bitrix_ingest.application.call_records import CallRecordsScanRequest, CallRecordsScanService
from bitrix_ingest.application.crm import CrmExportRequest, CrmExportService
from bitrix_ingest.application.date_range import (
    build_closed_filter,
    within_any_record_datetime_range,
    within_datetime_range,
)
from bitrix_ingest.application.whatsapp import WhatsAppExportRequest, WhatsAppExportService
from bitrix_ingest.application.whatsapp.export_service import _OutputDirectories
from bitrix_ingest.application.whatsapp_timeline import (
    WhatsAppTimelineExportRequest,
    WhatsAppTimelineExportService,
)


def test_build_closed_filter_includes_both_bounds():
    assert build_closed_filter(
        "DATE_MODIFY",
        date_from="2026-04-01",
        date_to="2026-04-30",
    ) == {
        ">=DATE_MODIFY": "2026-04-01T00:00:00",
        "<=DATE_MODIFY": "2026-04-30T23:59:59",
    }


def test_within_datetime_range_is_inclusive():
    assert within_datetime_range(
        "2026-04-30T23:59:59+03:00",
        date_from="2026-04-01",
        date_to="2026-04-30",
    )
    assert not within_datetime_range(
        "2026-05-01T00:00:00+03:00",
        date_from="2026-04-01",
        date_to="2026-04-30",
    )


def test_within_any_record_datetime_range_accepts_create_or_modify():
    assert within_any_record_datetime_range(
        {
            "DATE_CREATE": "2026-04-10T12:00:00+03:00",
            "DATE_MODIFY": "2025-12-01T12:00:00+03:00",
        },
        fields=("DATE_CREATE", "DATE_MODIFY"),
        date_from="2026-04-01",
        date_to="2026-04-30",
    )
    assert not within_any_record_datetime_range(
        {
            "DATE_CREATE": "2025-12-01T12:00:00+03:00",
            "DATE_MODIFY": "2025-12-02T12:00:00+03:00",
        },
        fields=("DATE_CREATE", "DATE_MODIFY"),
        date_from="2026-04-01",
        date_to="2026-04-30",
    )


class _CrmGateway:
    def __init__(self) -> None:
        self.list_calls: list[dict] = []

    def call(self, method: str, body: dict | None = None, label: str | None = None) -> dict:
        if method == "profile":
            return {"result": {"ID": "1", "NAME": "", "LAST_NAME": ""}}
        if method == "user.get":
            return {"result": []}
        raise AssertionError(f"unexpected method: {method}")

    def list_all(
        self,
        method: str,
        select: list[str],
        filter: dict | None = None,
        order: dict | None = None,
        context: str = "",
        limit: int | None = None,
    ) -> list[dict]:
        self.list_calls.append(
            {
                "method": method,
                "select": select,
                "filter": filter,
                "order": order,
                "context": context,
                "limit": limit,
            }
        )
        return []


def test_crm_export_applies_date_range_to_entities_and_activities(tmp_path):
    gateway = _CrmGateway()
    sink = MagicMock()
    service = CrmExportService(gateway=gateway, sink=sink)

    service.execute(
        CrmExportRequest(
            output_dir=tmp_path,
            date_from="2026-04-01",
            date_to="2026-04-30",
        )
    )

    entity_filters = {
        call["method"]: call["filter"]
        for call in gateway.list_calls
    }
    assert entity_filters["crm.deal.list"] == {
        ">=DATE_MODIFY": "2026-04-01T00:00:00",
        "<=DATE_MODIFY": "2026-04-30T23:59:59",
    }
    assert entity_filters["crm.activity.list"] == {
        ">=LAST_UPDATED": "2026-04-01T00:00:00",
        "<=LAST_UPDATED": "2026-04-30T23:59:59",
    }


class _CallGateway:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call(self, method: str, body: dict | None = None, label: str | None = None) -> dict:
        self.calls.append({"method": method, "body": body, "label": label})
        if method == "crm.activity.list":
            return {"result": []}
        raise AssertionError(f"unexpected method: {method}")


def test_call_scan_applies_date_range_to_activity_filter(tmp_path):
    gateway = _CallGateway()
    sink = MagicMock()
    service = CallRecordsScanService(gateway=gateway, sink=sink)

    service.execute(
        CallRecordsScanRequest(
            output_dir=tmp_path,
            limit=10,
            date_from="2026-04-01",
            date_to="2026-04-30",
        )
    )

    filter_ = gateway.calls[0]["body"]["filter"]
    assert filter_["TYPE_ID"] == "2"
    assert filter_[">=START_TIME"] == "2026-04-01T00:00:00"
    assert filter_["<=START_TIME"] == "2026-04-30T23:59:59"


class _DealGateway:
    def __init__(self, pages: dict[int, dict], *, page_delay: float = 0.0) -> None:
        self._pages = pages
        self.page_delay = page_delay
        self.calls: list[dict] = []

    def call(self, method: str, body: dict | None = None, label: str | None = None) -> dict:
        self.calls.append({"method": method, "body": body, "label": label})
        return self._pages[body["start"]]


def test_whatsapp_deal_scan_applies_date_range_to_deals(tmp_path):
    gateway = _DealGateway(
        {
            0: {
                "result": [
                    {
                        "ID": "51044",
                        "TITLE": "WhatsApp lead",
                        "CONTACT_ID": "64050",
                        "SOURCE_ID": "WZ-1",
                        "ASSIGNED_BY_ID": "5",
                        "STAGE_ID": "NEW",
                        "CATEGORY_ID": "0",
                        "DATE_CREATE": "2026-04-10T12:00:00+03:00",
                        "DATE_MODIFY": "2025-12-01T12:00:00+03:00",
                        "LAST_COMMUNICATION_TIME": "2026-04-10T12:00:00+03:00",
                    },
                    {
                        "ID": "51045",
                        "TITLE": "WhatsApp lead old",
                        "CONTACT_ID": "64051",
                        "SOURCE_ID": "WZ-2",
                        "ASSIGNED_BY_ID": "5",
                        "STAGE_ID": "NEW",
                        "CATEGORY_ID": "0",
                        "DATE_CREATE": "2025-11-10T12:00:00+03:00",
                        "DATE_MODIFY": "2025-12-01T12:00:00+03:00",
                        "LAST_COMMUNICATION_TIME": "2025-12-01T12:00:00+03:00",
                    },
                ],
            }
        }
    )
    sink = MagicMock()
    service = WhatsAppExportService(gateway=gateway, sink=sink)

    deals = service._load_whatsapp_deals(
        WhatsAppExportRequest(
            output_dir=tmp_path,
            limit=10,
            date_from="2026-04-01",
            date_to="2026-04-30",
        ),
        tmp_path,
    )

    assert [deal["ID"] for deal in deals] == ["51044"]
    # DATE_MODIFY bounds are now pushed to Bitrix API to avoid scanning all 4000+ deals.
    assert gateway.calls[0]["body"]["filter"] == {
        ">=DATE_MODIFY": "2026-04-01T00:00:00",
        "<=DATE_MODIFY": "2026-04-30T23:59:59",
    }


class _TimelineGateway:
    def __init__(self, deals: list[dict]) -> None:
        self.deals = deals
        self.list_calls: list[dict] = []

    def list_all(
        self,
        method: str,
        select: list[str],
        filter: dict | None = None,
        order: dict | None = None,
        context: str = "",
        limit: int | None = None,
    ) -> list[dict]:
        self.list_calls.append(
            {
                "method": method,
                "select": select,
                "filter": filter,
                "order": order,
                "context": context,
                "limit": limit,
            }
        )
        return list(self.deals)


def test_timeline_export_selects_deals_by_create_or_modify_date(tmp_path):
    gateway = _TimelineGateway(
        [
            {
                "ID": "70001",
                "TITLE": "WhatsApp lead",
                "CONTACT_ID": "64050",
                "SOURCE_ID": "WZ-1",
                "ASSIGNED_BY_ID": "5",
                "STAGE_ID": "NEW",
                "CATEGORY_ID": "2",
                "DATE_CREATE": "2026-04-10T12:00:00+03:00",
                "DATE_MODIFY": "2025-12-01T12:00:00+03:00",
                "LAST_COMMUNICATION_TIME": "2026-04-10T12:00:00+03:00",
            },
            {
                "ID": "70002",
                "TITLE": "WhatsApp old lead",
                "CONTACT_ID": "64051",
                "SOURCE_ID": "WZ-2",
                "ASSIGNED_BY_ID": "5",
                "STAGE_ID": "NEW",
                "CATEGORY_ID": "2",
                "DATE_CREATE": "2025-11-10T12:00:00+03:00",
                "DATE_MODIFY": "2025-12-01T12:00:00+03:00",
                "LAST_COMMUNICATION_TIME": "2025-12-01T12:00:00+03:00",
            },
        ]
    )
    sink = MagicMock()
    service = WhatsAppTimelineExportService(gateway=gateway, sink=sink)

    deals = service._load_whatsapp_deals(
        WhatsAppTimelineExportRequest(
            output_dir=tmp_path,
            limit=10,
            date_from="2026-04-01",
            date_to="2026-04-30",
        )
    )

    assert [deal["ID"] for deal in deals] == ["70001"]
    # DATE_MODIFY bounds are now pushed to Bitrix API to avoid scanning all deals.
    assert gateway.list_calls[0]["filter"] == {
        ">=DATE_MODIFY": "2026-04-01T00:00:00",
        "<=DATE_MODIFY": "2026-04-30T23:59:59",
    }


class _OpenlineGateway:
    def __init__(self, responses: dict[str, list[dict[str, object]]]) -> None:
        self._responses = {key: list(value) for key, value in responses.items()}
        self.calls: list[dict[str, object]] = []
        self.page_delay = 0.0

    def call(self, method: str, body: dict | None = None, label: str | None = None) -> dict:
        self.calls.append({"method": method, "body": body, "label": label})
        queue = self._responses.get(method)
        assert queue, f"unexpected method call: {method}"
        return queue.pop(0)


class _Sink:
    def __init__(self) -> None:
        self.documents: dict[str, object] = {}

    def write(self, path, data) -> None:
        self.documents[str(path)] = data


def test_whatsapp_conversation_filters_messages_by_date_range(tmp_path):
    gateway = _OpenlineGateway(
        {
            "imopenlines.crm.chat.get": [
                {
                    "result": [
                        {
                            "CHAT_ID": "64990",
                            "CONNECTOR_ID": "wz_whatsapp_connector",
                            "CONNECTOR_TITLE": "WAZZUP: WhatsApp",
                        }
                    ]
                }
            ],
            "imopenlines.dialog.get": [
                {
                    "result": {
                        "id": 64990,
                        "name": ". - WhatsApp",
                        "dialog_id": "chat64990",
                        "entity_data_1": "Y|DEAL|51044|N|N|26250|1776605548|0|0|0",
                    }
                }
            ],
            "imopenlines.session.history.get": [
                {
                    "result": {
                        "sessionId": 26250,
                        "message": {
                            "1": {
                                "id": "1",
                                "senderid": "21408",
                                "date": "2026-04-10T12:00:00+03:00",
                                "text": "inside range",
                                "params": {},
                            },
                            "2": {
                                "id": "2",
                                "senderid": "21408",
                                "date": "2026-05-01T12:00:00+03:00",
                                "text": "outside range",
                                "params": {},
                            },
                        },
                        "users": {
                            "21408": {
                                "id": "21408",
                                "name": "Client",
                                "connector": True,
                                "externalAuthId": "imconnector",
                            }
                        },
                        "files": [],
                    }
                }
            ],
        }
    )
    service = WhatsAppExportService(gateway=gateway, sink=_Sink())
    dirs = _OutputDirectories.prepare(tmp_path)

    conversation = service._build_conversation_with_fallback(
        {
            "ID": "51044",
            "TITLE": ". - WhatsApp",
            "CONTACT_ID": "64050",
            "SOURCE_ID": "WZ-1",
            "ASSIGNED_BY_ID": "5",
            "STAGE_ID": "NEW",
            "CATEGORY_ID": "0",
            "DATE_CREATE": "2026-04-19T16:32:29+03:00",
            "DATE_MODIFY": "2026-04-19T18:44:07+03:00",
            "LAST_COMMUNICATION_TIME": "2026-04-19T18:44:07+03:00",
        },
        "51044",
        dirs.paths_for("51044"),
        include_system_messages=True,
        date_from="2026-04-01",
        date_to="2026-04-30",
    )

    assert [message.text for message in conversation.messages] == ["inside range"]
