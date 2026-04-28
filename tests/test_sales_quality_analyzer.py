from __future__ import annotations

import json
from pathlib import Path

from bitrix_ingest.application.sales_quality import (
    AnalyzeSalesQualityRequest,
    AnalyzeSalesQualityService,
)


class FileSink:
    def write(self, path: str | Path, data):
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _feature_payload(*, good: bool) -> dict:
    if good:
        stages = {
            "contact_established": "yes",
            "need_identified": "yes",
            "product_presented": "yes",
            "offer_or_usp_mentioned": "yes",
            "next_step_attempted": "yes",
            "sale_attempted": "no",
        }
        problems = {
            "has_objections": False,
            "missing_qualification": False,
            "missing_next_step": False,
            "weak_presentation": False,
            "slow_response": False,
            "not_target_lead": False,
            "manager_did_not_ask_questions": False,
            "no_offer_or_usp": False,
            "client_negative_or_cold": False,
            "fragmented_or_low_content": False,
            "unclear_audio_or_text": False,
        }
        lead_status = "target"
        presentation_quality = "strong"
        next_status = "agreed"
    else:
        stages = {
            "contact_established": "yes",
            "need_identified": "no",
            "product_presented": "no",
            "offer_or_usp_mentioned": "no",
            "next_step_attempted": "no",
            "sale_attempted": "no",
        }
        problems = {
            "has_objections": True,
            "missing_qualification": True,
            "missing_next_step": True,
            "weak_presentation": True,
            "slow_response": False,
            "not_target_lead": False,
            "manager_did_not_ask_questions": True,
            "no_offer_or_usp": True,
            "client_negative_or_cold": True,
            "fragmented_or_low_content": False,
            "unclear_audio_or_text": False,
        }
        lead_status = "possibly_target"
        presentation_quality = "weak"
        next_status = "none"

    return {
        "summary": "Краткое резюме.",
        "lead_quality": {"status": lead_status, "reason": "По тексту диалога."},
        "sales_stages": stages,
        "qualification": {
            "need": stages["need_identified"],
            "budget": "unknown",
            "timeline": "unknown",
            "decision_maker": "unknown",
            "missing_fields": ["budget", "timeline"],
        },
        "presentation": {"quality": presentation_quality, "weaknesses": []},
        "objections": {
            "has_objections": "yes" if problems["has_objections"] else "no",
            "items": [],
        },
        "next_step": {
            "status": next_status,
            "description": "",
            "evidence": "",
        },
        "problems": problems,
        "evidence": ["evidence"],
        "tags": ["tag"],
        "manager_coaching": ["coaching"],
    }


class FakeResponsesGateway:
    def complete(self, **kwargs):
        prompt = kwargs["user_prompt"]
        good = '"source_type": "call"' in prompt
        return {
            "output_text": json.dumps(_feature_payload(good=good), ensure_ascii=False),
            "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        }

    @staticmethod
    def extract_output_text(response):
        return response["output_text"]


def test_sales_quality_analyzer_outputs_features_and_report(tmp_path):
    call_text = tmp_path / "call.txt"
    call_text.write_text("Здравствуйте, клиент хочет окна, менеджер назначил замер.", encoding="utf-8")
    manifest = tmp_path / "transcripts_manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "CRM_ACTIVITY_ID": "101",
                    "RECORD_FILE_ID": "202",
                    "TRANSCRIPT_STATUS": "transcribed",
                    "TRANSCRIPT_PATH": str(call_text),
                    "START_TIME": "2026-04-28T10:00:00+03:00",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    call_meta = tmp_path / "recording-candidates.json"
    call_meta.write_text(
        json.dumps(
            [
                {
                    "CRM_ACTIVITY_ID": "101",
                    "RESPONSIBLE_ID": "28",
                    "PHONE_NUMBER": "+70000000000",
                }
            ]
        ),
        encoding="utf-8",
    )
    activity_meta = tmp_path / "activities.source.json"
    activity_meta.write_text(
        json.dumps(
            [
                {
                    "ID": "101",
                    "OWNER_ID": "555",
                    "OWNER_TYPE_ID": "2",
                    "RESPONSIBLE_ID": "28",
                }
            ]
        ),
        encoding="utf-8",
    )

    wa_dir = tmp_path / "conversations_filtered"
    wa_dir.mkdir()
    (wa_dir / "deal_777.json").write_text(
        json.dumps(
            {
                "deal_id": "777",
                "contact_id": "888",
                "assigned_by_id": "36",
                "channel": "whatsapp",
                "stats": {
                    "first_manager_response_time_sec": 1200,
                    "avg_response_latency_sec": 1200,
                    "first_message_at": "2026-04-28T11:00:00+03:00",
                    "total_messages": 2,
                    "manager_messages": 1,
                    "client_messages": 1,
                },
                "messages": [
                    {
                        "created_at": "2026-04-28T11:00:00+03:00",
                        "sender_role": "client",
                        "text": "Сколько стоит?",
                    },
                    {
                        "created_at": "2026-04-28T11:20:00+03:00",
                        "sender_role": "manager",
                        "text": "Здравствуйте.",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    output_dir = tmp_path / "sales-quality"
    AnalyzeSalesQualityService(
        gateway=FakeResponsesGateway(),
        sink=FileSink(),
    ).execute(
        AnalyzeSalesQualityRequest(
            output_dir=output_dir,
            call_transcript_manifest_path=manifest,
            call_metadata_path=call_meta,
            activity_metadata_path=activity_meta,
            whatsapp_conversation_dir=wa_dir,
            slow_response_threshold_sec=900,
        )
    )

    report = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    assert report["total_interactions"] == 2
    assert report["overall_stage_score_pct"] == 60.0
    assert report["stage_funnel"][0]["pct"] == 100.0
    slow_problem = next(p for p in report["top_problems"] if p["key"] == "slow_response")
    assert slow_problem["count"] == 1
    assert (output_dir / "features" / "call_activity_101__record_202.json").exists()
    assert (output_dir / "features" / "whatsapp_deal_777.json").exists()
    assert (output_dir / "report.md").exists()
