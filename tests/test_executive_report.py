from __future__ import annotations

import json
from pathlib import Path

from bitrix_ingest.application.executive_report import (
    BuildExecutiveReportRequest,
    BuildExecutiveReportService,
)


class FileSink:
    def write(self, path: str | Path, data):
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class FakeGateway:
    def __init__(self):
        self.deals = [
            {
                "ID": "1",
                "TITLE": "Open deal",
                "STAGE_SEMANTIC_ID": "P",
                "STAGE_ID": "NEW",
                "CATEGORY_ID": "0",
                "ASSIGNED_BY_ID": "10",
                "OPPORTUNITY": "0",
                "CURRENCY_ID": "KZT",
                "CONTACT_ID": "101",
                "DATE_CREATE": "2026-04-01T10:00:00+03:00",
                "DATE_MODIFY": "2026-04-01T10:00:00+03:00",
            },
            {
                "ID": "2",
                "TITLE": "Won deal",
                "STAGE_SEMANTIC_ID": "S",
                "STAGE_ID": "WON",
                "CATEGORY_ID": "0",
                "ASSIGNED_BY_ID": "10",
                "OPPORTUNITY": "100000",
                "CURRENCY_ID": "KZT",
                "CONTACT_ID": "102",
                "DATE_CREATE": "2026-04-02T10:00:00+03:00",
                "DATE_MODIFY": "2026-04-02T10:00:00+03:00",
            },
            {
                "ID": "3",
                "TITLE": "Failed deal",
                "STAGE_SEMANTIC_ID": "F",
                "STAGE_ID": "LOSE",
                "CATEGORY_ID": "0",
                "ASSIGNED_BY_ID": "20",
                "OPPORTUNITY": "50000",
                "CURRENCY_ID": "KZT",
                "CONTACT_ID": "103",
                "DATE_CREATE": "2026-04-03T10:00:00+03:00",
                "DATE_MODIFY": "2026-04-03T10:00:00+03:00",
            },
        ]
        self.tasks = {
            "1": [
                {
                    "ID": "900",
                    "OWNER_ID": "1",
                    "OWNER_TYPE_ID": "2",
                    "PROVIDER_ID": "CRM_TASKS_TASK",
                    "RESPONSIBLE_ID": "10",
                    "DEADLINE": "2026-01-01T10:00:00+03:00",
                    "COMPLETED": "N",
                }
            ]
        }

    def call(self, method, body=None, label=None):
        if method == "user.get":
            return {
                "result": [
                    {"ID": "10", "NAME": "Manager", "LAST_NAME": "One"},
                    {"ID": "20", "NAME": "Manager", "LAST_NAME": "Two"},
                ]
            }
        raise AssertionError(method)

    def list_all(self, method, select, filter=None, order=None, context="", limit=None):
        filter = filter or {}
        if method == "crm.deal.list":
            ids = filter.get("ID")
            rows = self.deals
            if ids:
                allow = {str(v) for v in (ids if isinstance(ids, list) else [ids])}
                rows = [row for row in rows if row["ID"] in allow]
            return rows
        if method == "crm.status.list":
            return [
                {"STATUS_ID": "NEW", "NAME": "New"},
                {"STATUS_ID": "WON", "NAME": "Won"},
                {"STATUS_ID": "LOSE", "NAME": "Lose"},
            ]
        if method == "crm.contact.list":
            ids = filter.get("ID")
            allow = {str(v) for v in (ids if isinstance(ids, list) else [ids])}
            return [
                {
                    "ID": cid,
                    "NAME": f"Contact {cid}",
                    "PHONE": [{"VALUE": f"+7000{cid}"}],
                }
                for cid in allow
            ]
        if method == "crm.activity.list":
            deal_id = str(filter.get("OWNER_ID") or "")
            return self.tasks.get(deal_id, [])
        raise AssertionError(method)


def _write_sales_quality(tmp_path: Path) -> Path:
    root = tmp_path / "sales-quality"
    features = root / "features"
    features.mkdir(parents=True)
    (root / "report.json").write_text(
        json.dumps(
            {
                "overall_stage_score_pct": 50.0,
                "stage_funnel": [
                    {"key": "contact_established", "label": "Contact", "pct": 100.0}
                ],
                "top_problems": [
                    {
                        "key": "missing_next_step",
                        "label": "Нет следующего шага",
                        "count": 1,
                        "total": 3,
                        "pct": 33.3,
                    }
                ],
                "per_manager": [
                    {
                        "manager_id": "10",
                        "response_time": {
                            "known_count": 1,
                            "avg_first_response_time_sec": 600,
                            "slow_pct": 0,
                        },
                    },
                    {
                        "manager_id": "20",
                        "response_time": {
                            "known_count": 1,
                            "avg_first_response_time_sec": 1200,
                            "slow_pct": 100,
                        },
                    },
                ],
                "visuals": {"manager_heatmap": []},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for deal_id, failed in [("1", False), ("2", False), ("3", True)]:
        (features / f"deal_{deal_id}.json").write_text(
            json.dumps(
                {
                    "source": {"deal_id": deal_id, "manager_id": "20" if failed else "10"},
                    "lead_quality": {"status": "possibly_target"},
                    "problems": {
                        "missing_next_step": failed,
                        "no_offer_or_usp": failed,
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return root


def test_executive_report_builds_dashboard_tasks_and_lost_revenue(tmp_path):
    sq = _write_sales_quality(tmp_path)
    out = tmp_path / "executive"
    BuildExecutiveReportService(gateway=FakeGateway(), sink=FileSink()).execute(
        BuildExecutiveReportRequest(
            output_dir=out,
            sales_quality_dir=sq,
            average_ticket_kzt=200000,
            expected_conversion_pct=25,
        )
    )

    report = json.loads((out / "executive-report.json").read_text(encoding="utf-8"))
    assert report["deal_dashboard"]["department"]["in_work_count"] == 1
    assert report["deal_dashboard"]["department"]["won_amount"] == 100000
    assert report["deal_dashboard"]["department"]["failed_count"] == 1
    assert report["task_status"]["department"]["with_overdue_tasks"] == 1
    assert report["lost_revenue"]["estimated_lost_revenue_kzt"] == 50000
    assert report["failed_deal_reanimation"]["cards"][0]["deal_id"] == "3"
    assert report["data_readiness"][1]["status"] == "ready"
