"""GetCatalogService — fetches funnels, managers, and audit previews from Bitrix."""
from __future__ import annotations

import logging
from typing import Any

from ..date_range import build_closed_filter
from ..ports import BitrixGateway
from ..whatsapp.deal_filter import WhatsAppDealFilter

logger = logging.getLogger(__name__)

_DEAL_PREVIEW_SELECT = [
    "ID", "TITLE", "SOURCE_ID", "ASSIGNED_BY_ID", "CATEGORY_ID", "DATE_MODIFY",
]


class GetCatalogService:
    def __init__(self, gateway: BitrixGateway) -> None:
        self._gateway = gateway

    # ------------------------------------------------------------------
    # Dropdowns
    # ------------------------------------------------------------------

    def get_funnels(self) -> list[dict[str, Any]]:
        response = self._gateway.call("crm.dealcategory.list")
        items = response.get("result") or []
        rows = items if isinstance(items, list) else [items]
        funnels = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            funnels.append({
                "id": str(row.get("ID") or ""),
                "name": str(row.get("NAME") or ""),
                "sort": int(row.get("SORT") or 0),
            })
        logger.info("Funnels loaded: %d", len(funnels))
        return funnels

    def get_managers(self) -> list[dict[str, Any]]:
        response = self._gateway.call("user.get")
        items = response.get("result") or []
        rows = items if isinstance(items, list) else [items]
        managers = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name_parts = [
                str(row.get("NAME") or "").strip(),
                str(row.get("LAST_NAME") or "").strip(),
            ]
            full_name = " ".join(p for p in name_parts if p) or f"User {row.get('ID')}"
            managers.append({
                "id": str(row.get("ID") or ""),
                "name": full_name,
                "email": str(row.get("EMAIL") or ""),
                "active": row.get("ACTIVE") in (True, "Y", "1", 1),
            })
        logger.info("Managers loaded: %d", len(managers))
        return managers

    # ------------------------------------------------------------------
    # Audit preview — counts deals/managers before the full run
    # ------------------------------------------------------------------

    def get_audit_preview(
        self,
        funnel_ids: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        responsible_id: str | None = None,
    ) -> dict[str, Any]:
        """Return deal/manager counts for the given filter without exporting data."""
        deal_filter = build_closed_filter(
            "DATE_MODIFY", date_from=date_from, date_to=date_to
        )
        clean_funnels = [f for f in (funnel_ids or []) if f]
        if clean_funnels:
            deal_filter["CATEGORY_ID"] = clean_funnels if len(clean_funnels) > 1 else clean_funnels[0]
        if responsible_id:
            deal_filter["ASSIGNED_BY_ID"] = responsible_id

        all_deals = self._gateway.list_all(
            "crm.deal.list",
            select=_DEAL_PREVIEW_SELECT,
            filter=deal_filter,
            order={"DATE_MODIFY": "DESC"},
            context="audit preview",
        )

        wa_filter = WhatsAppDealFilter()
        wa_deals = wa_filter.select_whatsapp_deals(all_deals)

        manager_ids: set[str] = set()
        dates: list[str] = []
        for deal in wa_deals:
            mid = str(deal.get("ASSIGNED_BY_ID") or "").strip()
            if mid and mid != "0":
                manager_ids.add(mid)
            dm = str(deal.get("DATE_MODIFY") or "").strip()
            if dm:
                dates.append(dm)

        actual_from = min(dates) if dates else None
        actual_to = max(dates) if dates else None

        manager_index = self._build_manager_index()
        managers_in_scope = [
            manager_index.get(mid, {"id": mid, "name": mid})
            for mid in sorted(manager_ids)
        ]

        # Resolve funnel names
        funnels_in_scope: list[dict[str, Any]] = []
        if clean_funnels:
            all_funnels = self.get_funnels()
            funnels_index = {f["id"]: f for f in all_funnels}
            funnels_in_scope = [
                funnels_index.get(fid, {"id": fid, "name": fid})
                for fid in clean_funnels
            ]

        logger.info(
            "Audit preview: %d WhatsApp deals, %d managers, %d funnels (from %d total deals)",
            len(wa_deals), len(manager_ids), len(clean_funnels), len(all_deals),
        )
        return {
            "deal_count": len(wa_deals),
            "manager_count": len(manager_ids),
            "managers": managers_in_scope,
            "funnel_count": len(clean_funnels),
            "funnels": funnels_in_scope,
            "period_from": date_from or actual_from,
            "period_to": date_to or actual_to,
            "actual_date_from": actual_from,
            "actual_date_to": actual_to,
            "total_deals_scanned": len(all_deals),
        }

    def _build_manager_index(self) -> dict[str, dict[str, str]]:
        managers = self.get_managers()
        return {m["id"]: m for m in managers}
