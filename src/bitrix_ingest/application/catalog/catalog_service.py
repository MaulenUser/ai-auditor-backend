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
        scope_filter = build_closed_filter(
            "DATE_MODIFY", date_from=date_from, date_to=date_to
        )
        clean_funnels = [f for f in (funnel_ids or []) if f]
        if clean_funnels:
            scope_filter["CATEGORY_ID"] = clean_funnels if len(clean_funnels) > 1 else clean_funnels[0]

        scoped_deals = self._gateway.list_all(
            "crm.deal.list",
            select=_DEAL_PREVIEW_SELECT,
            filter=scope_filter,
            order={"DATE_MODIFY": "DESC"},
            context="audit preview",
        )

        wa_filter = WhatsAppDealFilter()
        scope_wa_deals = wa_filter.select_whatsapp_deals(scoped_deals)
        filtered_deals = self._filter_by_responsible(scoped_deals, responsible_id)
        wa_deals = wa_filter.select_whatsapp_deals(filtered_deals)

        manager_index = self._build_manager_index()
        scope_manager_ids = self._collect_manager_ids(scope_wa_deals)
        manager_ids = self._collect_manager_ids(wa_deals)
        dates: list[str] = []
        for deal in wa_deals:
            dm = str(deal.get("DATE_MODIFY") or "").strip()
            if dm:
                dates.append(dm)

        actual_from = min(dates) if dates else None
        actual_to = max(dates) if dates else None

        managers_in_scope = [
            manager_index.get(mid, {"id": mid, "name": mid})
            for mid in sorted(manager_ids)
        ]
        scope_managers = [
            manager_index.get(mid, {"id": mid, "name": mid})
            for mid in sorted(scope_manager_ids)
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

        warnings = self._build_preview_warnings(
            scoped_deals=scoped_deals,
            scope_wa_deals=scope_wa_deals,
            filtered_deals=filtered_deals,
            wa_deals=wa_deals,
            responsible_id=responsible_id,
            scope_managers=scope_managers,
            manager_index=manager_index,
        )

        logger.info(
            "Audit preview: %d WhatsApp deals, %d managers, %d funnels (from %d total deals)",
            len(wa_deals), len(manager_ids), len(clean_funnels), len(filtered_deals),
        )
        return {
            "deal_count": len(wa_deals),
            "manager_count": len(manager_ids),
            "managers": managers_in_scope,
            "scope_manager_count": len(scope_manager_ids),
            "scope_managers": scope_managers,
            "funnel_count": len(clean_funnels),
            "funnels": funnels_in_scope,
            "period_from": date_from or actual_from,
            "period_to": date_to or actual_to,
            "actual_date_from": actual_from,
            "actual_date_to": actual_to,
            "total_deals_scanned": len(filtered_deals),
            "requested_responsible_id": responsible_id,
            "responsible_filter_mode": "deal_owner",
            "warnings": warnings,
        }

    def _build_manager_index(self) -> dict[str, dict[str, str]]:
        managers = self.get_managers()
        return {m["id"]: m for m in managers}

    @staticmethod
    def _filter_by_responsible(
        deals: list[dict[str, Any]],
        responsible_id: str | None,
    ) -> list[dict[str, Any]]:
        if not responsible_id:
            return deals
        return [
            deal for deal in deals
            if str(deal.get("ASSIGNED_BY_ID") or "").strip() == responsible_id
        ]

    @staticmethod
    def _collect_manager_ids(deals: list[dict[str, Any]]) -> set[str]:
        manager_ids: set[str] = set()
        for deal in deals:
            manager_id = str(deal.get("ASSIGNED_BY_ID") or "").strip()
            if manager_id and manager_id != "0":
                manager_ids.add(manager_id)
        return manager_ids

    def _build_preview_warnings(
        self,
        *,
        scoped_deals: list[dict[str, Any]],
        scope_wa_deals: list[dict[str, Any]],
        filtered_deals: list[dict[str, Any]],
        wa_deals: list[dict[str, Any]],
        responsible_id: str | None,
        scope_managers: list[dict[str, Any]],
        manager_index: dict[str, dict[str, str]],
    ) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        if not scoped_deals:
            warnings.append({
                "code": "no_deals_in_scope",
                "message": "По выбранным воронкам и периоду Bitrix не вернул ни одной сделки.",
            })
            return warnings

        if not scope_wa_deals:
            warnings.append({
                "code": "no_whatsapp_deals_in_scope",
                "message": (
                    "Сделки в выбранной воронке и периоде есть, но WhatsApp-сделок среди них не найдено."
                ),
            })
            return warnings

        if not responsible_id:
            return warnings

        manager_name = manager_index.get(responsible_id, {}).get("name") or responsible_id
        if not filtered_deals:
            warnings.append({
                "code": "responsible_not_in_scope",
                "message": (
                    "Выбранный ответственный по сделке не найден в текущей выборке по воронке и периоду."
                ),
                "requested_responsible_id": responsible_id,
                "requested_responsible_name": manager_name,
                "available_scope_managers": scope_managers,
            })
            return warnings

        if not wa_deals:
            warnings.append({
                "code": "responsible_has_no_whatsapp_deals",
                "message": (
                    "У выбранного ответственного по сделке есть сделки в текущей выборке, "
                    "но среди них нет WhatsApp-сделок."
                ),
                "requested_responsible_id": responsible_id,
                "requested_responsible_name": manager_name,
                "available_scope_managers": scope_managers,
            })
        return warnings
