"""Tests for WhatsApp export deal selection and early-stop behaviour."""
from __future__ import annotations

from unittest.mock import MagicMock

from bitrix_ingest.application.whatsapp import WhatsAppExportRequest, WhatsAppExportService


def _deal(
    deal_id: str,
    *,
    date_modify: str,
    source_id: str = "",
    title: str = "Regular deal",
    contact_id: str = "0",
) -> dict[str, str]:
    return {
        "ID": deal_id,
        "TITLE": title,
        "CONTACT_ID": contact_id,
        "SOURCE_ID": source_id,
        "ASSIGNED_BY_ID": "1",
        "STAGE_ID": "NEW",
        "STAGE_SEMANTIC_ID": "P",
        "CATEGORY_ID": "0",
        "DATE_CREATE": date_modify,
        "DATE_MODIFY": date_modify,
        "LAST_COMMUNICATION_TIME": date_modify,
    }


class _FakeGateway:
    def __init__(self, pages: dict[int, dict], *, page_delay: float = 0.0) -> None:
        self._pages = pages
        self.page_delay = page_delay
        self.calls: list[dict] = []

    def call(self, method: str, body: dict | None = None, label: str | None = None) -> dict:
        assert method == "crm.deal.list"
        assert body is not None
        self.calls.append({"method": method, "body": body, "label": label})
        return self._pages[body["start"]]


def test_load_whatsapp_deals_stops_after_collecting_limit(tmp_path):
    gateway = _FakeGateway(
        {
            0: {
                "result": [
                    _deal("105", date_modify="2026-04-19T10:00:00+00:00"),
                    _deal("104", date_modify="2026-04-18T10:00:00+00:00", source_id="WZ-1"),
                ],
                "next": 50,
            },
            50: {
                "result": [
                    _deal("103", date_modify="2026-04-17T10:00:00+00:00"),
                    _deal("102", date_modify="2026-04-16T10:00:00+00:00", title="WhatsApp follow-up"),
                ],
                "next": 100,
            },
            100: {
                "result": [
                    _deal("101", date_modify="2026-04-15T10:00:00+00:00", source_id="WZ-2"),
                ],
            },
        }
    )
    sink = MagicMock()
    service = WhatsAppExportService(gateway=gateway, sink=sink)

    deals = service._load_whatsapp_deals(WhatsAppExportRequest(output_dir=tmp_path, limit=2), tmp_path)

    assert [deal["ID"] for deal in deals] == ["104", "102"]
    assert len(gateway.calls) == 2
    assert gateway.calls[0]["body"]["order"] == {"DATE_MODIFY": "DESC"}


def test_load_whatsapp_deals_respects_allowlist_before_stopping(tmp_path):
    gateway = _FakeGateway(
        {
            0: {
                "result": [
                    _deal("105", date_modify="2026-04-19T10:00:00+00:00", source_id="WZ-1"),
                    _deal("104", date_modify="2026-04-18T10:00:00+00:00"),
                ],
                "next": 50,
            },
            50: {
                "result": [
                    _deal("103", date_modify="2026-04-17T10:00:00+00:00", title="WhatsApp lead"),
                ],
            },
        }
    )
    sink = MagicMock()
    service = WhatsAppExportService(gateway=gateway, sink=sink)

    deals = service._load_whatsapp_deals(
        WhatsAppExportRequest(output_dir=tmp_path, limit=1, deal_ids=["103"]),
        tmp_path,
    )

    assert [deal["ID"] for deal in deals] == ["103"]
    assert len(gateway.calls) == 2


def test_load_whatsapp_deals_with_zero_limit_scans_all_pages(tmp_path):
    gateway = _FakeGateway(
        {
            0: {
                "result": [
                    _deal("105", date_modify="2026-04-19T10:00:00+00:00", source_id="WZ-1"),
                ],
                "next": 50,
            },
            50: {
                "result": [
                    _deal("104", date_modify="2026-04-18T10:00:00+00:00", title="WhatsApp chat"),
                ],
                "next": 100,
            },
            100: {
                "result": [
                    _deal("103", date_modify="2026-04-17T10:00:00+00:00", source_id="WZ-2"),
                ],
            },
        }
    )
    sink = MagicMock()
    service = WhatsAppExportService(gateway=gateway, sink=sink)

    deals = service._load_whatsapp_deals(WhatsAppExportRequest(output_dir=tmp_path, limit=0), tmp_path)

    assert [deal["ID"] for deal in deals] == ["105", "104", "103"]
    assert len(gateway.calls) == 3
