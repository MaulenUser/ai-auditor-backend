from __future__ import annotations

import json
from pathlib import Path

import pytest

from bitrix_ingest.application.audit.audit_service import RunAuditRequest, RunAuditService
from bitrix_ingest.infrastructure.audit_trace import AuditTraceRecorder


class DummyGateway:
    def call(self, method: str, body=None, label=None):  # pragma: no cover - should not be called
        raise AssertionError(f"Unexpected call: {method}")

    def list_all(self, method: str, select, filter=None, order=None, context="", limit=None):  # pragma: no cover
        raise AssertionError(f"Unexpected list_all: {method}")


class DummyResponsesGateway:
    def complete(self, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("OpenAI should not be called when timeline export has no messages")

    @staticmethod
    def extract_output_text(response):
        return ""


class DummyTranscriptionGateway:
    def transcribe(self, file_path, model, language, prompt):  # pragma: no cover - should not be called
        raise AssertionError("Transcription should not be called in this test")


class DummyDownloader:
    def download(self, url, target_path):  # pragma: no cover - should not be called
        raise AssertionError("Downloader should not be called in this test")


class FileSink:
    def write(self, path: str | Path, data):
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_report(output_dir: Path, *, total_messages: int, deal_id: str = "49334") -> None:
    (output_dir / "conversations").mkdir(parents=True, exist_ok=True)
    (output_dir / "raw").mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-04-20T00:00:00+00:00",
                "totals": {
                    "deals_scanned": 1,
                    "chats_exported": 1,
                    "total_messages": total_messages,
                },
                "rows": [
                    {
                        "deal_id": deal_id,
                        "total_messages": total_messages,
                        "output_file": str(output_dir / "conversations" / f"deal_{deal_id}.json"),
                    }
                ],
                "errors_count": 0,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_deals_source(output_dir: Path, deals: list[dict[str, object]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "deals.source.json").write_text(
        json.dumps(deals, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def test_run_audit_falls_back_to_timeline_when_openlines_is_empty(tmp_path, monkeypatch):
    import bitrix_ingest.application.audit.audit_service as audit_module

    timeline_calls: list[str] = []
    openlines_calls: list[str] = []

    def fake_timeline_execute(self, request):
        timeline_calls.append("timeline")
        _write_report(Path(request.output_dir), total_messages=3)

    def fake_openlines_execute(self, request):
        openlines_calls.append("openlines")
        _write_report(Path(request.output_dir), total_messages=0)

    def fake_features_execute(self, request):
        feature_dir = Path(request.output_dir)
        (feature_dir / "features").mkdir(parents=True, exist_ok=True)
        (feature_dir / "raw").mkdir(parents=True, exist_ok=True)
        (feature_dir / "features" / "deal_49334.json").write_text("{}", encoding="utf-8")
        (feature_dir / "errors.json").write_text("[]", encoding="utf-8")

    def fake_aggregate_execute(self, request):
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "aggregate.json").write_text('{"total": 1}', encoding="utf-8")

    def fake_recommendations_execute(self, request):
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "recommendations.json").write_text('{"score": 7}', encoding="utf-8")

    monkeypatch.setattr(audit_module.WhatsAppTimelineExportService, "execute", fake_timeline_execute)
    monkeypatch.setattr(audit_module.WhatsAppExportService, "execute", fake_openlines_execute)
    monkeypatch.setattr(audit_module.ExtractWhatsAppFeaturesService, "execute", fake_features_execute)
    monkeypatch.setattr(audit_module.AggregateFeatureService, "execute", fake_aggregate_execute)
    monkeypatch.setattr(audit_module.GenerateRecommendationsService, "execute", fake_recommendations_execute)

    service = RunAuditService(
        bitrix_gateway=DummyGateway(),
        responses_gateway=DummyResponsesGateway(),
        sink=FileSink(),
    )

    service.execute(
        RunAuditRequest(
            output_dir=tmp_path / "audit",
            funnel_ids=["2"],
            date_from="2026-01-01",
            date_to="2026-04-01",
        )
    )

    assert openlines_calls == ["openlines"]
    assert timeline_calls == ["timeline"]


def test_run_audit_raises_clear_error_when_openlines_and_timeline_are_empty(tmp_path, monkeypatch):
    import bitrix_ingest.application.audit.audit_service as audit_module

    def fake_openlines_execute(self, request):
        _write_report(Path(request.output_dir), total_messages=0)

    def fake_timeline_execute(self, request):
        _write_report(Path(request.output_dir), total_messages=0)

    monkeypatch.setattr(audit_module.WhatsAppExportService, "execute", fake_openlines_execute)
    monkeypatch.setattr(audit_module.WhatsAppTimelineExportService, "execute", fake_timeline_execute)

    service = RunAuditService(
        bitrix_gateway=DummyGateway(),
        responses_gateway=DummyResponsesGateway(),
        sink=FileSink(),
    )

    with pytest.raises(ValueError, match="all exported conversations are empty"):
        service.execute(
            RunAuditRequest(
                output_dir=tmp_path / "audit",
                funnel_ids=["2"],
                date_from="2026-01-01",
                date_to="2026-04-01",
            )
        )


def test_run_audit_executes_call_pipeline_when_crm_dependencies_are_present(tmp_path, monkeypatch):
    import bitrix_ingest.application.audit.audit_service as audit_module

    call_steps: list[str] = []

    def fake_openlines_execute(self, request):
        _write_report(Path(request.output_dir), total_messages=3)

    def fail_if_timeline_called(self, request):  # pragma: no cover - assertion helper
        raise AssertionError("Timeline export should not run when Open Lines already has messages")

    def fake_features_execute(self, request):
        feature_dir = Path(request.output_dir)
        (feature_dir / "features").mkdir(parents=True, exist_ok=True)
        (feature_dir / "raw").mkdir(parents=True, exist_ok=True)
        (feature_dir / "features" / "deal_49334.json").write_text("{}", encoding="utf-8")
        (feature_dir / "errors.json").write_text("[]", encoding="utf-8")

    def fake_aggregate_execute(self, request):
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "aggregate.json").write_text('{"total": 1}', encoding="utf-8")

    def fake_recommendations_execute(self, request):
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "recommendations.json").write_text('{"score": 7}', encoding="utf-8")

    def fake_call_scan_execute(self, request):
        call_steps.append("scan")
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "recording-candidates.json").write_text(
            json.dumps([
                {
                    "CRM_ACTIVITY_ID": "101",
                    "CALL_ID": "CALL-101",
                    "CALL_RECORD_URL": "https://cdn.example.com/101.mp3",
                    "RECORD_FILE_ID": "RF-101",
                    "RESPONSIBLE_ID": "32",
                    "PHONE_NUMBER": "+77770000000",
                    "START_TIME": "2026-04-01T10:00:00+05:00",
                    "SUBJECT": "Inbound",
                }
            ]),
            encoding="utf-8",
        )
        (output_dir / "activities.source.json").write_text(
            json.dumps([{"ID": "101", "DIRECTION": "1", "SUBJECT": "Inbound"}]),
            encoding="utf-8",
        )

    def fake_download_execute(self, request):
        call_steps.append("download")
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "manifest.json").write_text(
            json.dumps([
                {
                    "CRM_ACTIVITY_ID": "101",
                    "RECORD_FILE_ID": "RF-101",
                    "FILE_PATH": str(output_dir / "activity_101.mp3"),
                    "STATUS": "downloaded",
                }
            ]),
            encoding="utf-8",
        )

    def fake_transcribe_execute(self, request):
        call_steps.append("transcribe")
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "manifest.json").write_text(
            json.dumps([
                {
                    "CRM_ACTIVITY_ID": "101",
                    "RECORD_FILE_ID": "RF-101",
                    "TEXT_FILE_PATH": str(output_dir / "text" / "activity_101.txt"),
                    "STATUS": "transcribed",
                }
            ]),
            encoding="utf-8",
        )

    def fake_call_features_execute(self, request):
        call_steps.append("call_features")
        feature_dir = Path(request.output_dir)
        (feature_dir / "features").mkdir(parents=True, exist_ok=True)
        (feature_dir / "raw").mkdir(parents=True, exist_ok=True)
        (feature_dir / "features" / "activity_101.json").write_text("{}", encoding="utf-8")
        (feature_dir / "errors.json").write_text("[]", encoding="utf-8")

    monkeypatch.setattr(audit_module.WhatsAppExportService, "execute", fake_openlines_execute)
    monkeypatch.setattr(audit_module.WhatsAppTimelineExportService, "execute", fail_if_timeline_called)
    monkeypatch.setattr(audit_module.ExtractWhatsAppFeaturesService, "execute", fake_features_execute)
    monkeypatch.setattr(audit_module.AggregateFeatureService, "execute", fake_aggregate_execute)
    monkeypatch.setattr(audit_module.GenerateRecommendationsService, "execute", fake_recommendations_execute)
    monkeypatch.setattr(audit_module.CallRecordsScanService, "execute", fake_call_scan_execute)
    monkeypatch.setattr(audit_module.DownloadRecordingsService, "execute", fake_download_execute)
    monkeypatch.setattr(audit_module.TranscribeRecordingsService, "execute", fake_transcribe_execute)
    monkeypatch.setattr(audit_module.ExtractCallFeaturesService, "execute", fake_call_features_execute)

    service = RunAuditService(
        bitrix_gateway=DummyGateway(),
        responses_gateway=DummyResponsesGateway(),
        sink=FileSink(),
        call_gateway=DummyGateway(),
        transcription_gateway=DummyTranscriptionGateway(),
        file_downloader=DummyDownloader(),
    )

    service.execute(
        RunAuditRequest(
            output_dir=tmp_path / "audit",
            funnel_ids=["2"],
            date_from="2026-01-01",
            date_to="2026-04-01",
        )
    )

    assert call_steps == ["scan", "download", "transcribe", "call_features"]


def test_run_audit_skips_call_pipeline_without_crm_webhook(tmp_path, monkeypatch):
    import bitrix_ingest.application.audit.audit_service as audit_module

    def fake_openlines_execute(self, request):
        _write_report(Path(request.output_dir), total_messages=3)

    def fail_if_timeline_called(self, request):  # pragma: no cover - assertion helper
        raise AssertionError("Timeline export should not run when Open Lines already has messages")

    def fake_features_execute(self, request):
        feature_dir = Path(request.output_dir)
        (feature_dir / "features").mkdir(parents=True, exist_ok=True)
        (feature_dir / "raw").mkdir(parents=True, exist_ok=True)
        (feature_dir / "features" / "deal_49334.json").write_text("{}", encoding="utf-8")
        (feature_dir / "errors.json").write_text("[]", encoding="utf-8")

    def fake_aggregate_execute(self, request):
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "aggregate.json").write_text('{"total": 1}', encoding="utf-8")

    def fake_recommendations_execute(self, request):
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "recommendations.json").write_text('{"score": 7}', encoding="utf-8")

    def fail_if_called(self, request):  # pragma: no cover - assertion helper
        raise AssertionError("Call pipeline should be skipped when CRM webhook is absent")

    monkeypatch.setattr(audit_module.WhatsAppExportService, "execute", fake_openlines_execute)
    monkeypatch.setattr(audit_module.WhatsAppTimelineExportService, "execute", fail_if_timeline_called)
    monkeypatch.setattr(audit_module.ExtractWhatsAppFeaturesService, "execute", fake_features_execute)
    monkeypatch.setattr(audit_module.AggregateFeatureService, "execute", fake_aggregate_execute)
    monkeypatch.setattr(audit_module.GenerateRecommendationsService, "execute", fake_recommendations_execute)
    monkeypatch.setattr(audit_module.CallRecordsScanService, "execute", fail_if_called)

    service = RunAuditService(
        bitrix_gateway=DummyGateway(),
        responses_gateway=DummyResponsesGateway(),
        sink=FileSink(),
    )

    service.execute(
        RunAuditRequest(
            output_dir=tmp_path / "audit",
            funnel_ids=["2"],
            date_from="2026-01-01",
            date_to="2026-04-01",
        )
    )


def test_run_audit_writes_trace_file_with_stage_durations(tmp_path, monkeypatch):
    import bitrix_ingest.application.audit.audit_service as audit_module

    def fake_openlines_execute(self, request):
        _write_report(Path(request.output_dir), total_messages=3)

    def fail_if_timeline_called(self, request):  # pragma: no cover - assertion helper
        raise AssertionError("Timeline export should not run when Open Lines already has messages")

    def fake_features_execute(self, request):
        feature_dir = Path(request.output_dir)
        (feature_dir / "features").mkdir(parents=True, exist_ok=True)
        (feature_dir / "raw").mkdir(parents=True, exist_ok=True)
        (feature_dir / "features" / "deal_49334.json").write_text("{}", encoding="utf-8")
        (feature_dir / "errors.json").write_text("[]", encoding="utf-8")

    def fake_aggregate_execute(self, request):
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "aggregate.json").write_text('{"total": 1}', encoding="utf-8")

    def fake_recommendations_execute(self, request):
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "recommendations.json").write_text('{"score": 7}', encoding="utf-8")

    monkeypatch.setattr(audit_module.WhatsAppExportService, "execute", fake_openlines_execute)
    monkeypatch.setattr(audit_module.WhatsAppTimelineExportService, "execute", fail_if_timeline_called)
    monkeypatch.setattr(audit_module.ExtractWhatsAppFeaturesService, "execute", fake_features_execute)
    monkeypatch.setattr(audit_module.AggregateFeatureService, "execute", fake_aggregate_execute)
    monkeypatch.setattr(audit_module.GenerateRecommendationsService, "execute", fake_recommendations_execute)

    trace_path = tmp_path / "audit" / "audit-run.trace.json"
    trace = AuditTraceRecorder(
        trace_path,
        run_name="audit.run",
        request_details={"limit": 0},
    )
    service = RunAuditService(
        bitrix_gateway=DummyGateway(),
        responses_gateway=DummyResponsesGateway(),
        sink=FileSink(),
        trace=trace,
    )

    service.execute(
        RunAuditRequest(
            output_dir=tmp_path / "audit",
            funnel_ids=["2"],
            date_from="2026-01-01",
            date_to="2026-04-01",
        )
    )
    trace.finish(status="ok")

    payload = json.loads(trace_path.read_text(encoding="utf-8"))
    assert payload["run"]["status"] == "ok"
    stage_names = [event["name"] for event in payload["events"] if event["type"] == "stage"]
    assert "whatsapp_export" in stage_names
    assert "whatsapp_openlines_export" in stage_names
    assert "whatsapp_feature_extraction" in stage_names
    assert "feature_aggregation" in stage_names
    assert "recommendation_generation" in stage_names
    assert all(float(event["duration_ms"]) >= 0 for event in payload["events"])


def test_run_audit_writes_crm_outcome_summary(tmp_path, monkeypatch):
    import bitrix_ingest.application.audit.audit_service as audit_module

    def fake_openlines_execute(self, request):
        output_dir = Path(request.output_dir)
        _write_report(output_dir, total_messages=3)
        _write_deals_source(
            output_dir,
            [
                {
                    "ID": "1",
                    "ASSIGNED_BY_ID": "36",
                    "STAGE_ID": "WON",
                    "STAGE_SEMANTIC_ID": "S",
                    "OPPORTUNITY": "150000.00",
                },
                {
                    "ID": "2",
                    "ASSIGNED_BY_ID": "36",
                    "STAGE_ID": "LOSE",
                    "STAGE_SEMANTIC_ID": "F",
                    "OPPORTUNITY": "0.00",
                },
                {
                    "ID": "3",
                    "ASSIGNED_BY_ID": "32",
                    "STAGE_ID": "EXECUTING",
                    "STAGE_SEMANTIC_ID": "P",
                    "OPPORTUNITY": "275000.00",
                },
            ],
        )

    def fail_if_timeline_called(self, request):  # pragma: no cover - assertion helper
        raise AssertionError("Timeline export should not run when Open Lines already has messages")

    def fake_features_execute(self, request):
        feature_dir = Path(request.output_dir)
        (feature_dir / "features").mkdir(parents=True, exist_ok=True)
        (feature_dir / "raw").mkdir(parents=True, exist_ok=True)
        (feature_dir / "features" / "deal_1.json").write_text("{}", encoding="utf-8")
        (feature_dir / "errors.json").write_text("[]", encoding="utf-8")

    def fake_aggregate_execute(self, request):
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "aggregate.json").write_text('{"total": 1}', encoding="utf-8")

    def fake_recommendations_execute(self, request):
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "recommendations.json").write_text('{"score": 7}', encoding="utf-8")

    monkeypatch.setattr(audit_module.WhatsAppExportService, "execute", fake_openlines_execute)
    monkeypatch.setattr(audit_module.WhatsAppTimelineExportService, "execute", fail_if_timeline_called)
    monkeypatch.setattr(audit_module.ExtractWhatsAppFeaturesService, "execute", fake_features_execute)
    monkeypatch.setattr(audit_module.AggregateFeatureService, "execute", fake_aggregate_execute)
    monkeypatch.setattr(audit_module.GenerateRecommendationsService, "execute", fake_recommendations_execute)

    service = RunAuditService(
        bitrix_gateway=DummyGateway(),
        responses_gateway=DummyResponsesGateway(),
        sink=FileSink(),
    )

    service.execute(
        RunAuditRequest(
            output_dir=tmp_path / "audit",
            funnel_ids=["0"],
            date_from="2026-03-01",
            date_to="2026-04-07",
        )
    )

    payload = json.loads(
        (tmp_path / "audit" / "analytics" / "crm_outcome_summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["overall"] == {
        "total": 3,
        "successful": 1,
        "failed": 1,
        "in_progress": 1,
        "unknown": 0,
        "successful_amount": 150000.0,
        "failed_amount": 0.0,
        "in_progress_amount": 275000.0,
    }
    assert payload["by_manager"][0]["assigned_by_id"] == "36"
    assert payload["by_manager"][0]["successful"] == 1
    assert payload["by_manager"][0]["failed"] == 1
