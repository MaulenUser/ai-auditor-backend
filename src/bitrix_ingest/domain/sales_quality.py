"""Sales-quality analysis schema and labels."""
from __future__ import annotations

from typing import Any


SALES_STAGE_DEFINITIONS: tuple[dict[str, str], ...] = (
    {
        "key": "contact_established",
        "label": "Установление контакта",
    },
    {
        "key": "need_identified",
        "label": "Выявление потребности",
    },
    {
        "key": "product_presented",
        "label": "Презентация продукта",
    },
    {
        "key": "offer_or_usp_mentioned",
        "label": "Акции/УТП/офферы",
    },
    {
        "key": "next_step_or_sale",
        "label": "Следующий шаг или продажа",
    },
)


PROBLEM_DEFINITIONS: tuple[dict[str, str], ...] = (
    {"key": "has_objections", "label": "Есть возражения клиента"},
    {"key": "missing_qualification", "label": "Нет полноценной квалификации"},
    {"key": "missing_next_step", "label": "Нет следующего шага"},
    {"key": "weak_presentation", "label": "Слабая презентация"},
    {"key": "slow_response", "label": "Долгий ответ"},
    {"key": "not_target_lead", "label": "Нецелевой лид"},
    {"key": "manager_did_not_ask_questions", "label": "Менеджер не задает вопросы"},
    {"key": "no_offer_or_usp", "label": "Нет акции/УТП/оффера"},
    {"key": "client_negative_or_cold", "label": "Низкий интерес или негатив"},
    {"key": "fragmented_or_low_content", "label": "Мало полезного контента"},
    {"key": "unclear_audio_or_text", "label": "Плохое качество текста/аудио"},
)


def sales_quality_schema() -> dict[str, Any]:
    """Return strict JSON schema for OpenAI structured output."""
    yn = {"type": "string", "enum": ["yes", "no", "unknown"]}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "summary",
            "lead_quality",
            "sales_stages",
            "qualification",
            "presentation",
            "objections",
            "next_step",
            "problems",
            "evidence",
            "tags",
            "manager_coaching",
        ],
        "properties": {
            "summary": {
                "type": "string",
                "description": "Short factual summary in Russian.",
            },
            "lead_quality": {
                "type": "object",
                "additionalProperties": False,
                "required": ["status", "reason"],
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": [
                            "target",
                            "possibly_target",
                            "not_target",
                            "unknown",
                        ],
                    },
                    "reason": {"type": "string"},
                },
            },
            "sales_stages": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "contact_established",
                    "need_identified",
                    "product_presented",
                    "offer_or_usp_mentioned",
                    "next_step_attempted",
                    "sale_attempted",
                ],
                "properties": {
                    "contact_established": yn,
                    "need_identified": yn,
                    "product_presented": yn,
                    "offer_or_usp_mentioned": yn,
                    "next_step_attempted": yn,
                    "sale_attempted": yn,
                },
            },
            "qualification": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "need",
                    "budget",
                    "timeline",
                    "decision_maker",
                    "missing_fields",
                ],
                "properties": {
                    "need": yn,
                    "budget": yn,
                    "timeline": yn,
                    "decision_maker": yn,
                    "missing_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
            "presentation": {
                "type": "object",
                "additionalProperties": False,
                "required": ["quality", "weaknesses"],
                "properties": {
                    "quality": {
                        "type": "string",
                        "enum": ["strong", "acceptable", "weak", "missing", "unknown"],
                    },
                    "weaknesses": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
            "objections": {
                "type": "object",
                "additionalProperties": False,
                "required": ["has_objections", "items"],
                "properties": {
                    "has_objections": yn,
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["type", "text", "evidence"],
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": [
                                        "price",
                                        "timing",
                                        "need",
                                        "trust",
                                        "competitor",
                                        "availability",
                                        "delivery",
                                        "measurement",
                                        "callback",
                                        "no_interest",
                                        "other",
                                    ],
                                },
                                "text": {"type": "string"},
                                "evidence": {"type": "string"},
                            },
                        },
                    },
                },
            },
            "next_step": {
                "type": "object",
                "additionalProperties": False,
                "required": ["status", "description", "evidence"],
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["none", "proposed", "agreed", "unclear"],
                    },
                    "description": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
            "problems": {
                "type": "object",
                "additionalProperties": False,
                "required": [item["key"] for item in PROBLEM_DEFINITIONS],
                "properties": {
                    "has_objections": {"type": "boolean"},
                    "missing_qualification": {"type": "boolean"},
                    "missing_next_step": {"type": "boolean"},
                    "weak_presentation": {"type": "boolean"},
                    "slow_response": {"type": "boolean"},
                    "not_target_lead": {"type": "boolean"},
                    "manager_did_not_ask_questions": {"type": "boolean"},
                    "no_offer_or_usp": {"type": "boolean"},
                    "client_negative_or_cold": {"type": "boolean"},
                    "fragmented_or_low_content": {"type": "boolean"},
                    "unclear_audio_or_text": {"type": "boolean"},
                },
            },
            "evidence": {"type": "array", "items": {"type": "string"}},
            "tags": {"type": "array", "items": {"type": "string"}},
            "manager_coaching": {"type": "array", "items": {"type": "string"}},
        },
    }
