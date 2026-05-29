from __future__ import annotations

import json
from pathlib import Path

from bitrix_ingest.application.executive_pipeline import (
    RunExecutivePipelineRequest,
    RunExecutivePipelineService,
)


class FileSink:
    def write(self, path: str | Path, data):
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class DummyResponsesGateway:
    def complete(self, **kwargs):  # pragma: no cover - patched services do not call OpenAI
        raise AssertionError("OpenAI should not be called in this orchestration test")

    @staticmethod
    def extract_output_text(response):
        return ""


class DummyGateway:
    def __init__(self):
        self.deals = [
            {
                "ID": "1",
                "TITLE": "Inside scope - WhatsApp",
                "SOURCE_ID": "WZ-1",
                "CATEGORY_ID": "0",
                "ASSIGNED_BY_ID": "28",
                "DATE_CREATE": "2026-04-10T10:00:00+03:00",
                "DATE_MODIFY": "2026-05-10T10:00:00+03:00",
                "CLOSEDATE": "2026-05-15T10:00:00+03:00",
            },
            {
                "ID": "2",
                "TITLE": "Outside scope - WhatsApp",
                "SOURCE_ID": "WZ-1",
                "CATEGORY_ID": "0",
                "ASSIGNED_BY_ID": "28",
                "DATE_CREATE": "2026-05-10T10:00:00+03:00",
                "DATE_MODIFY": "2026-05-10T10:00:00+03:00",
                "CLOSEDATE": "2026-05-15T10:00:00+03:00",
            },
        ]

    def call(self, method: str, body=None, label=None):  # pragma: no cover
        raise AssertionError(f"Unexpected call: {method}")

    def list_all(self, method: str, select, filter=None, order=None, context="", limit=None):
        if method == "crm.deal.list":
            return self.deals
        raise AssertionError(f"Unexpected list_all: {method}")


class DummyTranscriptionGateway:
    def transcribe(self, file_path, model, language, prompt):  # pragma: no cover
        raise AssertionError("Transcription should not be called in this test")


class DummyDownloader:
    def download(self, url, destination):  # pragma: no cover
        raise AssertionError("Download should not be called in this test")


def test_executive_pipeline_uses_one_scope_for_sources_and_report(tmp_path, monkeypatch):
    import bitrix_ingest.application.executive_pipeline.run_service as pipeline_module

    captured: dict[str, object] = {}

    def fake_whatsapp_execute(self, request):
        captured["whatsapp_deal_ids"] = [row["ID"] for row in request.deal_rows]
        output_dir = Path(request.output_dir)
        (output_dir / "conversations").mkdir(parents=True, exist_ok=True)
        (output_dir / "report.json").write_text(
            json.dumps(
                {
                    "rows": [
                        {
                            "deal_id": "1",
                            "total_messages": 3,
                            "output_file": str(output_dir / "conversations" / "deal_1.json"),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (output_dir / "conversations" / "deal_1.json").write_text(
            json.dumps(
                {
                    "deal_id": "1",
                    "assigned_by_id": "28",
                    "messages": [
                        {"sender_role": "client", "text": "hello"},
                        {"sender_role": "manager", "text": "hi"},
                        {"sender_role": "system", "text": "hidden"},
                    ],
                }
            ),
            encoding="utf-8",
        )

    def fake_call_scan_execute(self, request):
        captured["call_deal_ids"] = request.deal_ids
        Path(request.output_dir).mkdir(parents=True, exist_ok=True)
        (Path(request.output_dir) / "recording-candidates.json").write_text("[]", encoding="utf-8")
        (Path(request.output_dir) / "activities.source.json").write_text("[]", encoding="utf-8")

    def fake_sales_quality_execute(self, request):
        output_dir = Path(request.output_dir)
        (output_dir / "features").mkdir(parents=True, exist_ok=True)
        (output_dir / "raw").mkdir(parents=True, exist_ok=True)
        (output_dir / "report.json").write_text(
            json.dumps({"stage_funnel": [], "per_manager": [], "top_problems": []}),
            encoding="utf-8",
        )
        (output_dir / "errors.json").write_text("[]", encoding="utf-8")

    def fake_build_report_execute(self, request):
        captured["report_date_from"] = request.date_from
        captured["report_category_ids"] = request.category_ids
        captured["report_responsible_ids"] = request.responsible_ids
        Path(request.output_dir).mkdir(parents=True, exist_ok=True)
        (Path(request.output_dir) / "executive-report.json").write_text(
            json.dumps({"scope": {"deals_loaded": 1}}),
            encoding="utf-8",
        )

    monkeypatch.setattr(pipeline_module.WhatsAppExportService, "execute", fake_whatsapp_execute)
    monkeypatch.setattr(pipeline_module.CallRecordsScanService, "execute", fake_call_scan_execute)
    monkeypatch.setattr(pipeline_module.AnalyzeSalesQualityService, "execute", fake_sales_quality_execute)
    monkeypatch.setattr(pipeline_module.BuildExecutiveReportService, "execute", fake_build_report_execute)

    service = RunExecutivePipelineService(
        crm_gateway=DummyGateway(),
        whatsapp_gateway=DummyGateway(),
        responses_gateway=DummyResponsesGateway(),
        transcription_gateway=DummyTranscriptionGateway(),
        file_downloader=DummyDownloader(),
        sink=FileSink(),
    )

    service.execute(
        RunExecutivePipelineRequest(
            sales_quality_dir=tmp_path / "sales-quality",
            executive_report_dir=tmp_path / "executive-report",
            whatsapp_dir=tmp_path / "whatsapp",
            call_scan_dir=tmp_path / "calls",
            recordings_dir=tmp_path / "recordings",
            date_from="2026-04-01",
            date_to="2026-04-30",
            category_ids=["0"],
            responsible_ids=["28"],
        )
    )

    assert captured["whatsapp_deal_ids"] == ["1"]
    assert captured["call_deal_ids"] == ["1"]
    assert captured["report_date_from"] == "2026-04-01"
    assert captured["report_category_ids"] == ["0"]
    assert captured["report_responsible_ids"] == ["28"]

    filtered = json.loads((tmp_path / "whatsapp" / "conversations_filtered" / "deal_1.json").read_text())
    assert [message["sender_role"] for message in filtered["messages"]] == ["client", "manager"]


def test_executive_pipeline_resume_reuses_existing_outputs(tmp_path, monkeypatch):
    import bitrix_ingest.application.executive_pipeline.run_service as pipeline_module

    captured: dict[str, object] = {}

    def fake_whatsapp_execute(self, request):
        output_dir = Path(request.output_dir)
        (output_dir / "conversations").mkdir(parents=True, exist_ok=True)
        (output_dir / "report.json").write_text(
            json.dumps(
                {
                    "rows": [
                        {
                            "deal_id": "1",
                            "total_messages": 2,
                            "output_file": str(output_dir / "conversations" / "deal_1.json"),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (output_dir / "conversations" / "deal_1.json").write_text(
            json.dumps(
                {
                    "deal_id": "1",
                    "messages": [
                        {"sender_role": "client", "text": "hello"},
                        {"sender_role": "manager", "text": "hi"},
                    ],
                }
            ),
            encoding="utf-8",
        )

    def fake_call_scan_execute(self, request):
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "recording-candidates.json").write_text(
            json.dumps([{"CRM_ACTIVITY_ID": "1"}]),
            encoding="utf-8",
        )
        (output_dir / "activities.source.json").write_text("[]", encoding="utf-8")

    def fake_download_execute(self, request):
        captured["download_skip_existing"] = request.skip_existing
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "manifest.json").write_text(
            json.dumps(
                [
                    {
                        "CRM_ACTIVITY_ID": "1",
                        "RECORD_FILE_ID": "2",
                        "FILE_PATH": str(output_dir / "call.mp3"),
                        "STATUS": "skipped_existing",
                    }
                ]
            ),
            encoding="utf-8",
        )

    def fake_transcribe_execute(self, request):
        captured["transcribe_skip_existing"] = request.skip_existing
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "manifest.json").write_text("[]", encoding="utf-8")

    def fake_sales_quality_execute(self, request):
        captured["sales_quality_skip_existing"] = request.skip_existing
        output_dir = Path(request.output_dir)
        (output_dir / "features").mkdir(parents=True, exist_ok=True)
        (output_dir / "raw").mkdir(parents=True, exist_ok=True)
        (output_dir / "report.json").write_text(
            json.dumps({"stage_funnel": [], "per_manager": [], "top_problems": []}),
            encoding="utf-8",
        )
        (output_dir / "errors.json").write_text("[]", encoding="utf-8")

    def fake_build_report_execute(self, request):
        Path(request.output_dir).mkdir(parents=True, exist_ok=True)
        (Path(request.output_dir) / "executive-report.json").write_text(
            json.dumps({"scope": {"deals_loaded": 1}}),
            encoding="utf-8",
        )

    monkeypatch.setattr(pipeline_module.WhatsAppExportService, "execute", fake_whatsapp_execute)
    monkeypatch.setattr(pipeline_module.CallRecordsScanService, "execute", fake_call_scan_execute)
    monkeypatch.setattr(pipeline_module.DownloadRecordingsService, "execute", fake_download_execute)
    monkeypatch.setattr(pipeline_module.TranscribeRecordingsService, "execute", fake_transcribe_execute)
    monkeypatch.setattr(pipeline_module.AnalyzeSalesQualityService, "execute", fake_sales_quality_execute)
    monkeypatch.setattr(pipeline_module.BuildExecutiveReportService, "execute", fake_build_report_execute)

    service = RunExecutivePipelineService(
        crm_gateway=DummyGateway(),
        whatsapp_gateway=DummyGateway(),
        responses_gateway=DummyResponsesGateway(),
        transcription_gateway=DummyTranscriptionGateway(),
        file_downloader=DummyDownloader(),
        sink=FileSink(),
    )

    service.execute(
        RunExecutivePipelineRequest(
            sales_quality_dir=tmp_path / "sales-quality",
            executive_report_dir=tmp_path / "executive-report",
            whatsapp_dir=tmp_path / "whatsapp",
            call_scan_dir=tmp_path / "calls",
            recordings_dir=tmp_path / "recordings",
            date_from="2026-04-01",
            date_to="2026-04-30",
            reset_outputs=False,
        )
    )

    assert captured["download_skip_existing"] is True
    assert captured["transcribe_skip_existing"] is True
    assert captured["sales_quality_skip_existing"] is True
