"""Tests for StageHistoryService and DealStageHistory domain entity."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bitrix_ingest.application.crm import StageHistoryRequest, StageHistoryService
from bitrix_ingest.domain.stage_history import DealStageHistory


# ---------------------------------------------------------------------------
# Domain entity tests
# ---------------------------------------------------------------------------

class TestDealStageHistoryFromRaw:
    def _rows(self) -> list[dict[str, Any]]:
        return [
            {
                "ID": "1001",
                "STAGE_ID": "NEW",
                "CREATED_TIME": "2026-01-01T10:00:00+03:00",
                "MOVED_BY_ID": "5",
            },
            {
                "ID": "1002",
                "STAGE_ID": "IN_PROCESS",
                "CREATED_TIME": "2026-01-02T10:00:00+03:00",
                "MOVED_BY_ID": "5",
            },
            {
                "ID": "1003",
                "STAGE_ID": "WON",
                "CREATED_TIME": "2026-01-03T10:00:00+03:00",
                "MOVED_BY_ID": "5",
            },
        ]

    def test_stage_count(self):
        history = DealStageHistory.from_raw(
            deal_id="99",
            rows=self._rows(),
            stage_names={"NEW": "Новая", "IN_PROCESS": "В работе", "WON": "Выиграна"},
        )
        assert history.total_stages_visited == 3

    def test_current_stage_is_last(self):
        history = DealStageHistory.from_raw(deal_id="99", rows=self._rows(), stage_names={})
        assert history.current_stage_id == "WON"

    def test_stage_names_resolved(self):
        history = DealStageHistory.from_raw(
            deal_id="99",
            rows=self._rows(),
            stage_names={"NEW": "Новая", "IN_PROCESS": "В работе", "WON": "Выиграна"},
        )
        assert history.stages[0].stage_name == "Новая"
        assert history.stages[1].stage_name == "В работе"

    def test_duration_computed_between_consecutive_entries(self):
        history = DealStageHistory.from_raw(deal_id="99", rows=self._rows(), stage_names={})
        # First stage: 2026-01-01 → 2026-01-02 = 24 hours
        assert history.stages[0].duration_hours == pytest.approx(24.0)
        # Second stage: 2026-01-02 → 2026-01-03 = 24 hours
        assert history.stages[1].duration_hours == pytest.approx(24.0)

    def test_last_stage_has_no_left_at(self):
        history = DealStageHistory.from_raw(deal_id="99", rows=self._rows(), stage_names={})
        assert history.stages[-1].left_at is None
        assert history.stages[-1].duration_hours is None

    def test_empty_rows_produce_empty_history(self):
        history = DealStageHistory.from_raw(deal_id="99", rows=[], stage_names={})
        assert history.total_stages_visited == 0
        assert history.current_stage_id is None

    def test_to_dict_shape(self):
        history = DealStageHistory.from_raw(
            deal_id="99",
            rows=self._rows()[:1],
            stage_names={"NEW": "Новая"},
        )
        d = history.to_dict()
        assert d["deal_id"] == "99"
        assert d["total_stages_visited"] == 1
        assert d["current_stage_id"] == "NEW"
        assert d["stages"][0]["stage_name"] == "Новая"
        assert d["stages"][0]["left_at"] is None

    def test_moved_by_id_captured(self):
        history = DealStageHistory.from_raw(deal_id="99", rows=self._rows(), stage_names={})
        assert history.stages[0].moved_by_id == "5"


# ---------------------------------------------------------------------------
# Service integration tests (with stub gateway)
# ---------------------------------------------------------------------------

class _Gateway:
    def __init__(
        self,
        deals: list[dict[str, Any]],
        history_by_deal: dict[str, list[dict[str, Any]]],
        stages: list[dict[str, Any]] | None = None,
    ) -> None:
        self._deals = deals
        self._history = history_by_deal
        self._stages = stages or []

    def call(self, method: str, body: dict | None = None, label: str | None = None) -> dict:
        if method == "crm.status.list":
            return {"result": self._stages}
        raise AssertionError(f"unexpected call: {method}")

    def list_all(
        self,
        method: str,
        select: list[str],
        filter: dict | None = None,
        order: dict | None = None,
        context: str = "",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if method == "crm.deal.list":
            return list(self._deals)
        if method == "crm.stagehistory.list":
            deal_id = str((filter or {}).get("ENTITY_ID", ""))
            return self._history.get(deal_id, [])
        raise AssertionError(f"unexpected list_all: {method}")


class _Sink:
    def __init__(self) -> None:
        self.documents: dict[str, Any] = {}

    def write(self, path: str | Path, data: Any) -> None:
        self.documents[str(path)] = data


def _deal(deal_id: str) -> dict[str, Any]:
    return {
        "ID": deal_id,
        "TITLE": f"Deal {deal_id} - WhatsApp",
        "CONTACT_ID": "1000",
        "SOURCE_ID": f"WZ-{deal_id}",
        "ASSIGNED_BY_ID": "5",
        "STAGE_ID": "WON",
        "CATEGORY_ID": "0",
        "DATE_CREATE": "2026-03-01T10:00:00+03:00",
        "DATE_MODIFY": "2026-03-15T10:00:00+03:00",
    }


def test_service_writes_per_deal_history_file(tmp_path):
    gateway = _Gateway(
        deals=[_deal("100")],
        history_by_deal={
            "100": [
                {"STAGE_ID": "NEW", "CREATED_TIME": "2026-03-01T10:00:00+03:00", "MOVED_BY_ID": "5"},
                {"STAGE_ID": "WON", "CREATED_TIME": "2026-03-15T10:00:00+03:00", "MOVED_BY_ID": "5"},
            ]
        },
    )
    sink = _Sink()
    service = StageHistoryService(gateway=gateway, sink=sink)
    service.execute(StageHistoryRequest(output_dir=tmp_path))

    history_key = str(tmp_path / "histories" / "deal_100.json")
    assert history_key in sink.documents
    doc = sink.documents[history_key]
    assert doc["deal_id"] == "100"
    assert doc["total_stages_visited"] == 2
    assert doc["stages"][0]["stage_id"] == "NEW"
    assert doc["stages"][1]["stage_id"] == "WON"


def test_service_writes_report_and_errors(tmp_path):
    gateway = _Gateway(deals=[_deal("101")], history_by_deal={"101": []})
    sink = _Sink()
    service = StageHistoryService(gateway=gateway, sink=sink)
    service.execute(StageHistoryRequest(output_dir=tmp_path))

    assert str(tmp_path / "report.json") in sink.documents
    assert str(tmp_path / "errors.json") in sink.documents
    report = sink.documents[str(tmp_path / "report.json")]
    assert report["deals_processed"] == 1


def test_service_writes_catalog_stages(tmp_path):
    gateway = _Gateway(
        deals=[],
        history_by_deal={},
        stages=[
            {"STATUS_ID": "NEW", "NAME": "Новая", "SORT": 10},
            {"STATUS_ID": "WON", "NAME": "Выиграна", "SORT": 20},
        ],
    )
    sink = _Sink()
    service = StageHistoryService(gateway=gateway, sink=sink)
    service.execute(StageHistoryRequest(output_dir=tmp_path))

    catalog_key = str(tmp_path / "catalog_stages.json")
    assert catalog_key in sink.documents
    stages = sink.documents[catalog_key]
    stage_ids = [s["id"] for s in stages]
    assert "NEW" in stage_ids
    assert "WON" in stage_ids


def test_service_writes_all_histories_summary(tmp_path):
    gateway = _Gateway(
        deals=[_deal("200"), _deal("201")],
        history_by_deal={
            "200": [{"STAGE_ID": "NEW", "CREATED_TIME": "2026-03-01T10:00:00+03:00", "MOVED_BY_ID": "5"}],
            "201": [{"STAGE_ID": "WON", "CREATED_TIME": "2026-03-05T10:00:00+03:00", "MOVED_BY_ID": "7"}],
        },
    )
    sink = _Sink()
    service = StageHistoryService(gateway=gateway, sink=sink)
    service.execute(StageHistoryRequest(output_dir=tmp_path))

    all_key = str(tmp_path / "all_histories.json")
    assert all_key in sink.documents
    all_histories = sink.documents[all_key]
    assert len(all_histories) == 2
    deal_ids = {h["deal_id"] for h in all_histories}
    assert deal_ids == {"200", "201"}


def test_service_respects_whatsapp_only_filter(tmp_path):
    gateway = _Gateway(
        deals=[
            _deal("300"),  # SOURCE_ID = WZ-300 → WhatsApp
            {
                **_deal("301"),
                "SOURCE_ID": "CALL",  # not WhatsApp
                "TITLE": "Regular deal",
            },
        ],
        history_by_deal={"300": [], "301": []},
    )
    sink = _Sink()
    service = StageHistoryService(gateway=gateway, sink=sink)
    service.execute(StageHistoryRequest(output_dir=tmp_path, whatsapp_only=True))

    report = sink.documents[str(tmp_path / "report.json")]
    assert report["deals_processed"] == 1
    assert str(tmp_path / "histories" / "deal_300.json") in sink.documents
    assert str(tmp_path / "histories" / "deal_301.json") not in sink.documents


def test_service_limit_is_respected(tmp_path):
    deals = [_deal(str(i)) for i in range(10)]
    history_by_deal = {str(i): [] for i in range(10)}
    gateway = _Gateway(deals=deals, history_by_deal=history_by_deal)
    sink = _Sink()
    service = StageHistoryService(gateway=gateway, sink=sink)
    service.execute(StageHistoryRequest(output_dir=tmp_path, limit=3))

    report = sink.documents[str(tmp_path / "report.json")]
    assert report["deals_processed"] == 3
