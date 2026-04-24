"""StageHistoryService — exports crm.stagehistory.list per deal."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...domain.stage_history import DealStageHistory
from ..catalog.catalog_service import GetCatalogService
from ..date_range import within_any_record_datetime_range
from ..ports import BitrixGateway, JsonSink
from ..whatsapp.deal_filter import WhatsAppDealFilter

logger = logging.getLogger(__name__)

_DEAL_SELECT = [
    "ID", "TITLE", "CONTACT_ID", "SOURCE_ID", "ASSIGNED_BY_ID",
    "STAGE_ID", "STAGE_SEMANTIC_ID", "CATEGORY_ID", "DATE_CREATE", "DATE_MODIFY",
    "OPPORTUNITY", "CURRENCY_ID", "CLOSEDATE", "CLOSED",
    "LOSS_REASON_ID",
]
_HISTORY_SELECT = [
    "ID", "TYPE_ID", "OWNER_TYPE_ID", "OWNER_ID",
    "ENTITY_TYPE_ID", "ENTITY_ID", "ITEM_ID",
    "STAGE_ID", "CREATED_TIME", "MOVE_TIME",
    "MOVED_BY_ID", "CREATED_BY_ID",
]


@dataclass(frozen=True)
class StageHistoryRequest:
    output_dir: Path
    category_ids: list[str] | None = None
    deal_ids: list[str] | None = None
    date_from: str | None = None
    date_to: str | None = None
    responsible_id: str | None = None
    whatsapp_only: bool = False
    limit: int = 0
    skip_existing: bool = False


class StageHistoryService:
    """Fetches stage transition history for deals and writes per-deal JSON files."""

    def __init__(self, gateway: BitrixGateway, sink: JsonSink) -> None:
        self._gateway = gateway
        self._sink = sink

    def execute(self, request: StageHistoryRequest) -> None:
        raw_dir = request.output_dir / "raw"
        histories_dir = request.output_dir / "histories"
        for d in (request.output_dir, raw_dir, histories_dir):
            d.mkdir(parents=True, exist_ok=True)

        # Build stage name catalog once.
        stage_names = self._build_stage_names(request.category_ids)
        self._sink.write(request.output_dir / "catalog_stages.json", list(stage_names.values()))

        deals = self._load_deals(request)
        self._sink.write(request.output_dir / "deals.source.json", deals)
        logger.info("Deals to process: %d", len(deals))

        errors: list[dict[str, Any]] = []
        all_histories: list[dict[str, Any]] = []

        for deal in deals:
            deal_id = str(deal.get("ID", ""))
            history_path = histories_dir / f"deal_{deal_id}.json"

            if request.skip_existing and history_path.exists():
                logger.info("Skipped deal ID=%s (history already exists)", deal_id)
                continue

            try:
                history = self._export_deal_history(
                    deal_id=deal_id,
                    raw_path=raw_dir / f"deal_{deal_id}.stagehistory.json",
                    stage_names=stage_names,
                )
                self._sink.write(history_path, history.to_dict())
                all_histories.append(history.to_dict())
                logger.info(
                    "Stage history for deal ID=%s: %d transitions",
                    deal_id,
                    history.total_stages_visited,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({"deal_id": deal_id, "message": str(exc)})
                logger.warning("Failed deal ID=%s: %s", deal_id, exc)

        report = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "deals_processed": len(all_histories),
            "errors_count": len(errors),
        }
        self._sink.write(request.output_dir / "report.json", report)
        self._sink.write(request.output_dir / "errors.json", errors)
        self._sink.write(request.output_dir / "all_histories.json", all_histories)

        logger.info(
            "Stage history export done. Deals: %d, errors: %d. Output: %s",
            len(all_histories),
            len(errors),
            request.output_dir.resolve(),
        )

    # ------------------------------------------------------------------
    # Internal steps
    # ------------------------------------------------------------------

    def _export_deal_history(
        self,
        *,
        deal_id: str,
        raw_path: Path,
        stage_names: dict[str, dict[str, str]],
    ) -> DealStageHistory:
        rows = self._gateway.list_all(
            "crm.stagehistory.list",
            select=_HISTORY_SELECT,
            filter={"ENTITY_TYPE_ID": 2, "ENTITY_ID": int(deal_id)},
            order={"CREATED_TIME": "ASC"},
            context=f"deal ID={deal_id} stage history",
        )
        self._sink.write(raw_path, rows)

        flat_names: dict[str, str] = {sid: info["name"] for sid, info in stage_names.items()}
        return DealStageHistory.from_raw(deal_id=deal_id, rows=rows, stage_names=flat_names)

    def _load_deals(self, request: StageHistoryRequest) -> list[dict[str, Any]]:
        deal_filter: dict[str, Any] = {}
        clean = [f for f in (request.category_ids or []) if f]
        if clean:
            deal_filter["CATEGORY_ID"] = clean if len(clean) > 1 else clean[0]
        if request.responsible_id:
            deal_filter["ASSIGNED_BY_ID"] = request.responsible_id

        all_deals = self._gateway.list_all(
            "crm.deal.list",
            select=_DEAL_SELECT,
            filter=deal_filter,
            order={"DATE_MODIFY": "DESC"},
            context="stage history deal source",
        )

        if request.date_from or request.date_to:
            all_deals = [
                d for d in all_deals
                if within_any_record_datetime_range(
                    d,
                    fields=("DATE_CREATE", "DATE_MODIFY"),
                    date_from=request.date_from,
                    date_to=request.date_to,
                )
            ]

        if request.whatsapp_only:
            all_deals = WhatsAppDealFilter().select_whatsapp_deals(all_deals)

        if request.deal_ids:
            allow = set(request.deal_ids)
            all_deals = [d for d in all_deals if str(d.get("ID", "")) in allow]

        if request.limit > 0:
            all_deals = all_deals[: request.limit]

        return all_deals

    def _build_stage_names(
        self,
        category_ids: list[str] | None,
    ) -> dict[str, dict[str, str]]:
        """Return {stage_id: {id, name, entity_id}} map from crm.status.list."""
        catalog = GetCatalogService(self._gateway)
        stages = catalog.get_stages(category_ids)
        return {s["id"]: s for s in stages}
