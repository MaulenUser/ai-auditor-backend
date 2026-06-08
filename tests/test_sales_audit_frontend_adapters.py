from __future__ import annotations

from bitrix_ingest.application.sales_audit.frontend_adapters import (
    build_frontend_sales_audit_data,
)
from bitrix_ingest.application.sales_audit.report_builder import build_sales_audit_report


def _feature(
    *,
    source_type: str,
    source_id: str,
    deal_id: str,
    manager_name: str = "Alice",
    first_response_time_sec: int | None = None,
    need_identified: str = "yes",
    product_presented: str = "yes",
    next_step_status: str = "agreed",
    missing_next_step: bool = False,
) -> dict:
    return {
        "summary": "Клиент задал вопрос, менеджер ответил.",
        "lead_quality": {"status": "target", "reason": "Целевой клиент"},
        "sales_stages": {
            "contact_established": "yes",
            "need_identified": need_identified,
            "product_presented": product_presented,
            "offer_or_usp_mentioned": "no",
            "next_step_attempted": "yes" if next_step_status in {"agreed", "proposed"} else "no",
            "sale_attempted": "no",
        },
        "qualification": {
            "need": need_identified,
            "budget": "unknown",
            "timeline": "unknown",
            "decision_maker": "unknown",
            "missing_fields": [],
        },
        "presentation": {"quality": "acceptable", "weaknesses": []},
        "objections": {"has_objections": "no", "items": []},
        "next_step": {"status": next_step_status, "description": "", "evidence": ""},
        "problems": {
            "has_objections": False,
            "missing_qualification": need_identified != "yes",
            "missing_next_step": missing_next_step,
            "weak_presentation": False,
            "slow_response": bool(first_response_time_sec and first_response_time_sec > 1800),
            "not_target_lead": False,
            "manager_did_not_ask_questions": need_identified != "yes",
            "no_offer_or_usp": True,
            "client_negative_or_cold": False,
            "fragmented_or_low_content": False,
            "unclear_audio_or_text": False,
        },
        "evidence": ["Клиент спрашивал цену"],
        "tags": ["цена"],
        "manager_coaching": ["Назначить следующий шаг"],
        "stage_flags": {
            "contact_established": True,
            "need_identified": need_identified == "yes",
            "product_presented": product_presented == "yes",
            "offer_or_usp_mentioned": False,
            "next_step_or_sale": next_step_status in {"agreed", "proposed"},
        },
        "stage_score_pct": 60.0,
        "response_time": {
            "first_response_time_sec": first_response_time_sec,
            "avg_response_latency_sec": first_response_time_sec,
            "slow_response_threshold_sec": 900,
            "is_slow_response": bool(first_response_time_sec and first_response_time_sec > 1800),
        },
        "source": {
            "source_type": source_type,
            "source_id": source_id,
            "source_file_path": f"export/{source_type}/{source_id}.txt",
            "channel": source_type,
            "manager_id": "8",
            "manager_name": manager_name,
            "deal_id": deal_id,
            "crm_activity_id": source_id if source_type == "call" else "",
            "record_file_id": "rec-1" if source_type == "call" else "",
            "contact_id": "99",
            "started_at": "2026-06-08T11:00:00+05:00",
        },
    }


def test_frontend_adapter_builds_interaction_index_and_urgent_alerts() -> None:
    features = [
        _feature(
            source_type="whatsapp",
            source_id="777",
            deal_id="777",
            first_response_time_sec=2400,
            need_identified="no",
            next_step_status="none",
            missing_next_step=True,
        ),
        _feature(source_type="call", source_id="101", deal_id="555"),
    ]
    report = {
        "task_status": {
            "deals": [
                {
                    "deal_id": "777",
                    "manager_id": "8",
                    "manager_name": "Alice",
                    "active_task_count": 0,
                    "overdue_task_count": 1,
                }
            ]
        }
    }
    scope_deals = [
        {
            "ID": "777",
            "TITLE": "Оптовая заявка",
            "ASSIGNED_BY_ID": "8",
            "STAGE_SEMANTIC_ID": "P",
        },
        {
            "ID": "555",
            "TITLE": "Звонок по замеру",
            "ASSIGNED_BY_ID": "8",
            "STAGE_SEMANTIC_ID": "P",
        },
    ]

    data = build_frontend_sales_audit_data(
        features=features,
        report=report,
        scope_deals=scope_deals,
        portal_base_url="https://example.bitrix24.kz",
    )

    assert len(data["interaction_index"]) == 2
    whatsapp = data["whatsapp_interactions"][0]
    assert whatsapp["channel"] == "whatsapp"
    assert whatsapp["deal_title"] == "Оптовая заявка"
    assert whatsapp["response_wait_minutes"] == 40.0
    assert whatsapp["need_identified"] == "no"
    assert data["call_interactions"][0]["transcript_file_path"] == "export/call/101.txt"

    alert = data["urgent_alerts"][0]
    assert alert["deal_id"] == "777"
    assert alert["deal_url"] == "https://example.bitrix24.kz/crm/deal/details/777/"
    assert {trigger["type"] for trigger in alert["triggers"]} == {
        "response_sla",
        "no_next_step",
        "no_active_task",
        "overdue_task",
        "no_need_identified",
    }
    assert alert["trigger_type"] == "response_sla"


def test_frontend_adapter_resolves_manager_name_from_report_references() -> None:
    data = build_frontend_sales_audit_data(
        features=[
            _feature(
                source_type="whatsapp",
                source_id="777",
                deal_id="777",
                manager_name="",
            ),
        ],
        report={"references": {"manager_names": {"8": "Alice Manager"}}},
        scope_deals=[
            {
                "ID": "777",
                "TITLE": "РћРїС‚РѕРІР°СЏ Р·Р°СЏРІРєР°",
                "ASSIGNED_BY_ID": "8",
                "STAGE_SEMANTIC_ID": "P",
            },
        ],
    )

    row = data["whatsapp_interactions"][0]
    assert row["manager_id"] == "8"
    assert row["manager_name"] == "Alice Manager"
    assert row["source"]["manager_name"] == "Alice Manager"


def test_sales_audit_report_includes_frontend_arrays() -> None:
    report = build_sales_audit_report(
        executive_report={},
        sales_report={
            "run_id": "run-1",
            "tenant_id": "tenant",
            "deal_dashboard": {"department": {"failed_count": 0}},
            "task_status": {"department": {}, "deals": []},
        },
        sales_quality_features=[
            _feature(source_type="whatsapp", source_id="777", deal_id="777"),
        ],
        scope_deals=[{"ID": "777", "TITLE": "Оптовая заявка"}],
        portal_base_url="https://example.bitrix24.kz",
    )

    assert report["interaction_index"][0]["deal_title"] == "Оптовая заявка"
    assert report["whatsapp_interactions"][0]["deal_id"] == "777"
    assert report["call_interactions"] == []
    assert report["alerts_dashboard"]["rows"] == report["urgent_alerts"]
