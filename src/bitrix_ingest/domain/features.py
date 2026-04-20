"""Feature extraction domain entities and OpenAI JSON schemas."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# JSON schemas for OpenAI structured output
# ---------------------------------------------------------------------------

_SHARED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary", "primary_topic", "client_request", "relevance_to_company",
        "qualification", "sales_process", "objections", "outcome", "sentiment",
        "quality_flags", "tags",
    ],
    "properties": {
        "summary": {"type": "string", "description": "Short factual summary in Russian."},
        "primary_topic": {"type": "string", "description": "Main topic in Russian."},
        "client_request": {"type": "string", "description": "What the client wanted, in Russian."},
        "relevance_to_company": {
            "type": "string",
            "enum": ["target_client", "not_target_client", "unclear"],
        },
        "qualification": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "need_identified", "budget_discussed",
                "timeline_discussed", "decision_maker_identified",
            ],
            "properties": {
                "need_identified": {"type": "string", "enum": ["yes", "no", "unknown"]},
                "budget_discussed": {"type": "string", "enum": ["yes", "no", "unknown"]},
                "timeline_discussed": {"type": "string", "enum": ["yes", "no", "unknown"]},
                "decision_maker_identified": {"type": "string", "enum": ["yes", "no", "unknown"]},
            },
        },
        "sales_process": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "manager_introduced_self", "manager_asked_questions",
                "manager_presented_service", "manager_rushed_to_pitch",
                "manager_agreed_next_step",
            ],
            "properties": {
                "manager_introduced_self": {"type": "string", "enum": ["yes", "no", "unknown"]},
                "manager_asked_questions": {"type": "string", "enum": ["yes", "no", "unknown"]},
                "manager_presented_service": {"type": "string", "enum": ["yes", "no", "unknown"]},
                "manager_rushed_to_pitch": {"type": "string", "enum": ["yes", "no", "unknown"]},
                "manager_agreed_next_step": {"type": "string", "enum": ["yes", "no", "unknown"]},
            },
        },
        "objections": {
            "type": "object",
            "additionalProperties": False,
            "required": ["has_objections", "items"],
            "properties": {
                "has_objections": {"type": "string", "enum": ["yes", "no", "unknown"]},
                "items": {"type": "array", "items": {"type": "string"}},
            },
        },
        "outcome": {
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "next_step", "next_step_confirmed"],
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [
                        "qualified_interest", "callback_requested", "follow_up",
                        "not_target_client", "not_interested", "technical_or_short",
                        "other", "unknown",
                    ],
                },
                "next_step": {"type": "string"},
                "next_step_confirmed": {"type": "string", "enum": ["yes", "no", "unknown"]},
            },
        },
        "sentiment": {
            "type": "object",
            "additionalProperties": False,
            "required": ["client_interest_level", "client_emotion"],
            "properties": {
                "client_interest_level": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "unknown"],
                },
                "client_emotion": {
                    "type": "string",
                    "enum": ["negative", "neutral", "positive", "unknown"],
                },
            },
        },
        "quality_flags": {
            "type": "object",
            "additionalProperties": False,
            "required": ["short_or_low_content", "audio_or_transcript_unclear", "non_sales_call"],
            "properties": {
                "short_or_low_content": {"type": "boolean"},
                "audio_or_transcript_unclear": {"type": "boolean"},
                "non_sales_call": {"type": "boolean"},
            },
        },
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}


def call_features_schema() -> dict[str, Any]:
    return copy.deepcopy(_SHARED_SCHEMA)


def whatsapp_features_schema() -> dict[str, Any]:
    schema = copy.deepcopy(_SHARED_SCHEMA)
    schema["properties"]["outcome"]["properties"]["status"]["enum"] = [
        "qualified_interest", "callback_requested", "follow_up", "awaiting_response",
        "not_target_client", "not_interested", "technical_or_short", "other", "unknown",
    ]
    schema["properties"]["quality_flags"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["short_or_low_content", "fragmented_chat", "non_sales_chat"],
        "properties": {
            "short_or_low_content": {"type": "boolean"},
            "fragmented_chat": {"type": "boolean"},
            "non_sales_chat": {"type": "boolean"},
        },
    }
    return schema


# ---------------------------------------------------------------------------
# Report row entities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallFeatureReportRow:
    crm_activity_id: str
    record_file_id: str
    feature_file_path: str
    raw_file_path: str
    status: str
    primary_topic: str
    relevance: str
    outcome_status: str
    client_interest: str
    short_or_low_content: bool
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "CRM_ACTIVITY_ID": self.crm_activity_id,
            "RECORD_FILE_ID": self.record_file_id,
            "FEATURE_FILE_PATH": self.feature_file_path,
            "RAW_FILE_PATH": self.raw_file_path,
            "STATUS": self.status,
            "PRIMARY_TOPIC": self.primary_topic,
            "RELEVANCE": self.relevance,
            "OUTCOME_STATUS": self.outcome_status,
            "CLIENT_INTEREST": self.client_interest,
            "SHORT_OR_LOW_CONTENT": self.short_or_low_content,
            "INPUT_TOKENS": self.input_tokens,
            "OUTPUT_TOKENS": self.output_tokens,
            "TOTAL_TOKENS": self.total_tokens,
            "CACHED_TOKENS": self.cached_tokens,
            "REASONING_TOKENS": self.reasoning_tokens,
        }


@dataclass(frozen=True)
class WhatsAppFeatureReportRow:
    deal_id: str
    contact_id: str
    feature_file_path: str
    raw_file_path: str
    status: str
    primary_topic: str
    relevance: str
    outcome_status: str
    client_interest: str
    short_or_low_content: bool
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "DEAL_ID": self.deal_id,
            "CONTACT_ID": self.contact_id,
            "FEATURE_FILE_PATH": self.feature_file_path,
            "RAW_FILE_PATH": self.raw_file_path,
            "STATUS": self.status,
            "PRIMARY_TOPIC": self.primary_topic,
            "RELEVANCE": self.relevance,
            "OUTCOME_STATUS": self.outcome_status,
            "CLIENT_INTEREST": self.client_interest,
            "SHORT_OR_LOW_CONTENT": self.short_or_low_content,
            "INPUT_TOKENS": self.input_tokens,
            "OUTPUT_TOKENS": self.output_tokens,
            "TOTAL_TOKENS": self.total_tokens,
            "CACHED_TOKENS": self.cached_tokens,
            "REASONING_TOKENS": self.reasoning_tokens,
        }
