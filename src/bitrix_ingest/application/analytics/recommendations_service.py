"""GenerateRecommendationsService — dept-level recommendations from aggregate stats."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ...domain.openai_usage import extract_usage_event, summarize_usage
from ..ports import JsonSink

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a senior sales analytics expert reviewing aggregated statistics \
from a sales department.

Based on the data provided, produce a structured analysis in Russian:
1. key_findings — 3-7 specific observations backed by the numbers.
2. patterns — recurring behavioral patterns (positive and negative).
3. manager_insights — which manager IDs stand out and why (skip if per_manager is absent).
4. recommendations — concrete, prioritised actions the team should take.
5. overall_assessment — one paragraph summary of department health.
6. score — an integer 1-10 rating of overall sales effectiveness.

Rules:
- Cite exact percentages from the statistics.
- Be specific, not generic.
- All text fields must be in Russian.
- Follow the JSON schema exactly."""

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "key_findings", "patterns", "manager_insights",
        "recommendations", "overall_assessment", "score",
    ],
    "properties": {
        "key_findings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific findings backed by statistics, in Russian.",
        },
        "patterns": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["pattern", "frequency", "impact"],
                "properties": {
                    "pattern": {"type": "string"},
                    "frequency": {"type": "string"},
                    "impact": {"type": "string", "enum": ["positive", "negative", "neutral"]},
                },
            },
        },
        "manager_insights": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["manager_id", "insight"],
                "properties": {
                    "manager_id": {"type": "string"},
                    "insight": {"type": "string"},
                },
            },
        },
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "description", "priority"],
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        },
        "overall_assessment": {"type": "string"},
        "score": {"type": "integer"},
    },
}


class ResponsesGateway(Protocol):
    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]: ...

    @staticmethod
    def extract_output_text(response: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class GenerateRecommendationsRequest:
    aggregate_path: Path
    output_dir: Path
    model: str = "gpt-4o"
    source_label: str = ""


class GenerateRecommendationsService:
    def __init__(self, gateway: ResponsesGateway, sink: JsonSink) -> None:
        self._gateway = gateway
        self._sink = sink

    def execute(self, request: GenerateRecommendationsRequest) -> None:
        if not request.aggregate_path.exists():
            raise FileNotFoundError(
                f"Aggregate file not found: {request.aggregate_path}"
            )

        aggregate = json.loads(request.aggregate_path.read_text(encoding="utf-8"))
        total = aggregate.get("total", 0)
        if not total:
            raise ValueError("Aggregate contains no data (total=0).")

        label = request.source_label or str(request.aggregate_path.parent.name)
        user_prompt = (
            f"Source: {label}\n"
            f"Total items analysed: {total}\n\n"
            f"Aggregated statistics:\n"
            f"{json.dumps(aggregate, ensure_ascii=False, indent=2)}"
        )

        logger.info(
            "Generating recommendations for %d items via %s", total, request.model
        )
        response = self._gateway.complete(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model=request.model,
            schema_name="sales_recommendations",
            schema=_SCHEMA,
        )
        parsed = json.loads(self._gateway.extract_output_text(response))

        event = extract_usage_event(
            response,
            stage="recommendations",
            entity_type="aggregate",
            entity_id=label,
            source_file_path=str(request.aggregate_path),
            model=request.model,
        )
        usage_events = [event] if event else []

        result = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "source_label": label,
            "total_items": total,
            "model": request.model,
            **parsed,
        }

        request.output_dir.mkdir(parents=True, exist_ok=True)
        self._sink.write(request.output_dir / "recommendations.json", result)
        self._sink.write(request.output_dir / "recommendations.raw.json", response)
        self._sink.write(request.output_dir / "usage-events.json", usage_events)
        self._sink.write(
            request.output_dir / "usage-summary.json", summarize_usage(usage_events)
        )

        logger.info("Recommendations generated.")
        logger.info("Score: %s/10", parsed.get("score"))
        logger.info("Key findings: %d", len(parsed.get("key_findings") or []))
        logger.info("Recommendations: %d", len(parsed.get("recommendations") or []))
        logger.info("Files saved to %s", request.output_dir.resolve())
