"""RunAuditService — full AI audit pipeline in one call.

Pipeline:
  1. WhatsApp timeline export  (Bitrix API, filtered by funnel/period/manager)
  2. WhatsApp feature extraction  (OpenAI)
  3. Feature aggregation  (local computation)
  4. Recommendation generation  (OpenAI)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..analytics import AggregateFeatureRequest, AggregateFeatureService
from ..analytics import GenerateRecommendationsRequest, GenerateRecommendationsService
from ..ports import BitrixGateway, JsonSink
from ..whatsapp_features import ExtractWhatsAppFeaturesRequest, ExtractWhatsAppFeaturesService
from ..whatsapp_timeline import WhatsAppTimelineExportRequest, WhatsAppTimelineExportService

logger = logging.getLogger(__name__)


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
class RunAuditRequest:
    output_dir: Path
    funnel_ids: list[str] | None = None  # None = all funnels; list = OR across selected
    date_from: str | None = None
    date_to: str | None = None
    responsible_id: str | None = None
    limit: int = 0
    model: str = "gpt-4o-mini"
    recommendations_model: str = "gpt-4o"
    source_label: str = ""


class RunAuditService:
    """Chains WhatsApp timeline → features → aggregate → recommendations."""

    def __init__(
        self,
        bitrix_gateway: BitrixGateway,
        responses_gateway: ResponsesGateway,
        sink: JsonSink,
    ) -> None:
        self._bitrix = bitrix_gateway
        self._responses = responses_gateway
        self._sink = sink

    def execute(self, request: RunAuditRequest) -> None:
        timeline_dir = request.output_dir / "whatsapp-timeline"
        features_dir = request.output_dir / "whatsapp-features"
        analytics_dir = request.output_dir / "analytics"

        logger.info("=== AUDIT STEP 1/4: WhatsApp timeline export ===")
        WhatsAppTimelineExportService(
            gateway=self._bitrix, sink=self._sink,
        ).execute(
            WhatsAppTimelineExportRequest(
                output_dir=timeline_dir,
                limit=request.limit,
                date_from=request.date_from,
                date_to=request.date_to,
                category_ids=request.funnel_ids,
                responsible_id=request.responsible_id,
            )
        )

        logger.info("=== AUDIT STEP 2/4: WhatsApp feature extraction ===")
        ExtractWhatsAppFeaturesService(
            gateway=self._responses, sink=self._sink,
        ).execute(
            ExtractWhatsAppFeaturesRequest(
                output_dir=features_dir,
                conversation_report_path=timeline_dir / "report.json",
                conversation_dir=timeline_dir / "conversations",
                model=request.model,
                limit=0,
                skip_existing=False,
            )
        )

        logger.info("=== AUDIT STEP 3/4: Feature aggregation ===")
        AggregateFeatureService(sink=self._sink).execute(
            AggregateFeatureRequest(
                features_dir=features_dir / "features",
                output_dir=analytics_dir,
                limit=0,
            )
        )

        label = request.source_label or _build_label(request)
        logger.info("=== AUDIT STEP 4/4: Recommendation generation ===")
        GenerateRecommendationsService(
            gateway=self._responses, sink=self._sink,
        ).execute(
            GenerateRecommendationsRequest(
                aggregate_path=analytics_dir / "aggregate.json",
                output_dir=analytics_dir,
                model=request.recommendations_model,
                source_label=label,
            )
        )

        logger.info("=== AUDIT COMPLETE. Output: %s ===", request.output_dir.resolve())


def _build_label(request: RunAuditRequest) -> str:
    parts: list[str] = []
    if request.funnel_ids:
        parts.append("funnels_" + "_".join(request.funnel_ids))
    if request.date_from or request.date_to:
        parts.append(f"{request.date_from or 'start'}_{request.date_to or 'now'}")
    if request.responsible_id:
        parts.append(f"manager_{request.responsible_id}")
    return "_".join(parts) if parts else "full_audit"
