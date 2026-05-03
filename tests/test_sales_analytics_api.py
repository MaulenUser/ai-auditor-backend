from __future__ import annotations

from fastapi.testclient import TestClient

from bitrix_ingest.api import app as app_module


class _FakeSalesRepo:
    def build_report(self, *, tenant_id, run_id):
        return {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "meta": {},
            "deal_dashboard": {},
            "task_status": {},
            "lead_status": {},
            "revenue_summary": {},
            "failure_reasons": {},
            "references": {},
        }

    def get_sales_audit_report(self, *, tenant_id, run_id):
        return None


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(app_module, "_DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(app_module, "_sales_repo", lambda: _FakeSalesRepo())
    monkeypatch.delenv("AI_AUDITOR_AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("AUTH_REQUIRED", raising=False)
    return TestClient(app_module.app)


def test_sales_analytics_run_wait_persists_run_and_reads_report(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    calls: dict[str, object] = {}

    def fake_execute_sales_analytics_pipeline(**kwargs):
        calls.update(kwargs)
        return {
            "tenant_id": kwargs["tenant_id"],
            "run_id": kwargs["run_id"],
            "storage": "postgres",
            "summary": {"meta": {"deals_unique": "3"}},
        }

    monkeypatch.setattr(
        app_module,
        "_execute_sales_analytics_pipeline",
        fake_execute_sales_analytics_pipeline,
    )

    response = client.post(
        "/sales-analytics/run",
        data={
            "date_from": "2026-04-01",
            "date_to": "2026-05-03",
            "include_tasks": "false",
            "include_leads": "false",
            "include_revenue": "false",
            "wait": "true",
        },
        headers={"X-Webhook-Url": "https://example.bitrix24.kz/rest/1/token/"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["storage"] == "postgres"
    assert payload["report"]["summary"]["meta"]["deals_unique"] == "3"
    assert calls["date_from"] == "2026-04-01"
    assert calls["date_to"] == "2026-05-03"
    assert calls["include_tasks"] is False
    assert calls["tenant_id"] == "default"


def test_sales_analytics_report_requires_existing_postgres_run(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.get(
        "/sales-analytics/report",
        params={"run_id": "missing"},
    )

    assert response.status_code == 404


def test_sales_audit_run_wait_returns_unified_report(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    calls: dict[str, object] = {}

    def fake_execute_sales_audit_pipeline(**kwargs):
        calls.update(kwargs)
        return {
            "report": {
                "generated_at": "2026-05-03T00:00:00+00:00",
                "scope": {"mode": "sales_audit"},
                "integral_rating": {"score_10": 6.8},
            }
        }

    monkeypatch.setattr(
        app_module,
        "_execute_sales_audit_pipeline",
        fake_execute_sales_audit_pipeline,
    )

    response = client.post(
        "/sales-audit/run",
        data={
            "date_from": "2026-04-01",
            "date_to": "2026-05-03",
            "wait": "true",
        },
        headers={
            "X-Webhook-Url": "https://example.bitrix24.kz/rest/1/token/",
            "X-Whatsapp-Webhook-Url": "https://example.bitrix24.kz/rest/1/token/",
            "X-OpenAI-Api-Key": "sk-test",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["storage"] == "postgres"
    assert payload["executive_report"]["integral_rating"]["score_10"] == 6.8
    assert calls["tenant_id"] == "default"
    assert calls["date_from"] == "2026-04-01"
