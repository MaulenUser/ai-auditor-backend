"""GetCatalogService — fetches funnels, managers, and audit previews from Bitrix."""
from __future__ import annotations

import logging
from typing import Any

from ..date_range import build_closed_filter, within_datetime_range
from ..ports import BitrixGateway
from ..whatsapp.deal_filter import WhatsAppDealFilter

logger = logging.getLogger(__name__)

_ITEM_DEAL_PREVIEW_SELECT = [
    "id", "title", "sourceId", "assignedById", "categoryId", "createdTime", "updatedTime",
]
_ITEM_DEAL_PREVIEW_ORDER = {"updatedTime": "DESC", "id": "DESC"}


class GetCatalogService:
    def __init__(self, gateway: BitrixGateway) -> None:
        self._gateway = gateway

    # ------------------------------------------------------------------
    # Dropdowns
    # ------------------------------------------------------------------

    def get_funnels(self) -> list[dict[str, Any]]:
        response = self._gateway.call(
            "crm.category.list",
            body={"entityTypeId": 2},
        )
        items = (response.get("result") or {}).get("categories") or []
        rows = items if isinstance(items, list) else [items]
        funnels = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            funnel_id = row.get("id")
            if funnel_id is None:
                funnel_id = row.get("ID")
            name = row.get("name")
            if name is None:
                name = row.get("NAME")
            sort = row.get("sort")
            if sort is None:
                sort = row.get("SORT")
            funnels.append({
                "id": str(funnel_id or "") if funnel_id != 0 else "0",
                "name": str(name or ""),
                "sort": int(sort or 0),
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
        clean_funnels = list(dict.fromkeys(f for f in (funnel_ids or []) if f))
        scoped_deals = self._load_preview_deals(
            category_ids=clean_funnels or None,
            date_from=date_from,
            date_to=date_to,
        )

        wa_filter = WhatsAppDealFilter()
        scope_wa_deals = wa_filter.select_whatsapp_deals(scoped_deals)
        filtered_deals = self._load_preview_deals(
            category_ids=clean_funnels or None,
            date_from=date_from,
            date_to=date_to,
            responsible_id=responsible_id,
        ) if responsible_id else scoped_deals
        wa_deals = wa_filter.select_whatsapp_deals(filtered_deals)

        manager_index = self._build_manager_index()
        scope_manager_ids = self._collect_manager_ids(scope_wa_deals)
        manager_ids = self._collect_manager_ids(wa_deals)
        dates = self._collect_scope_dates(
            wa_deals,
            date_from=date_from,
            date_to=date_to,
        )

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

    def _load_preview_deals(
        self,
        *,
        category_ids: list[str] | None,
        date_from: str | None,
        date_to: str | None,
        responsible_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not category_ids:
            return self._list_item_deals(
                category_id=None,
                date_from=date_from,
                date_to=date_to,
                responsible_id=responsible_id,
                context="audit preview all funnels",
            )

        deals: list[dict[str, Any]] = []
        for category_id in category_ids:
            deals.extend(
                self._list_item_deals(
                    category_id=category_id,
                    date_from=date_from,
                    date_to=date_to,
                    responsible_id=responsible_id,
                    context=f"audit preview funnel {category_id}",
                )
            )
        return deals

    def _list_item_deals(
        self,
        *,
        category_id: str | None,
        date_from: str | None,
        date_to: str | None,
        responsible_id: str | None,
        context: str,
    ) -> list[dict[str, Any]]:
        filter_ = self._build_item_filter(
            category_id=category_id,
            responsible_id=responsible_id,
            date_from=date_from,
            date_to=date_to,
        )
        deals: list[dict[str, Any]] = []
        start = 0
        page_num = 1

        while True:
            label = f"crm.item.list {context} page {page_num} (start={start})"
            response = self._gateway.call(
                "crm.item.list",
                body={
                    "entityTypeId": 2,
                    "select": _ITEM_DEAL_PREVIEW_SELECT,
                    "filter": filter_,
                    "order": _ITEM_DEAL_PREVIEW_ORDER,
                    "start": start,
                },
                label=label,
            )

            result = response.get("result") or {}
            rows = (result.get("items") or []) if isinstance(result, dict) else []
            if not isinstance(rows, list):
                rows = [rows]

            deals.extend(self._normalize_item_deal(row) for row in rows if isinstance(row, dict))
            logger.info(
                "%s: loaded %d rows, total %d",
                label,
                len(rows),
                len(deals),
            )

            next_start = response.get("next")
            if next_start is None:
                logger.info(
                    "crm.item.list %s pagination completed on page %d. Total rows: %d",
                    context,
                    page_num,
                    len(deals),
                )
                return deals

            start = int(next_start)
            page_num += 1

    @staticmethod
    def _build_item_filter(
        *,
        category_id: str | None,
        responsible_id: str | None,
        date_from: str | None,
        date_to: str | None,
    ) -> dict[str, Any]:
        filter_: dict[str, Any] = {}
        if category_id is not None:
            filter_["categoryId"] = GetCatalogService._coerce_numeric_id(category_id)
        if responsible_id:
            filter_["assignedById"] = GetCatalogService._coerce_numeric_id(responsible_id)

        created_filter = build_closed_filter(
            "createdTime",
            date_from=date_from,
            date_to=date_to,
        )
        updated_filter = build_closed_filter(
            "updatedTime",
            date_from=date_from,
            date_to=date_to,
        )
        if created_filter and updated_filter:
            filter_[0] = {
                "logic": "OR",
                0: created_filter,
                1: updated_filter,
            }
        elif created_filter:
            filter_.update(created_filter)
        elif updated_filter:
            filter_.update(updated_filter)
        return filter_

    @staticmethod
    def _normalize_item_deal(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "ID": GetCatalogService._stringify_value(row.get("id")),
            "TITLE": str(row.get("title") or ""),
            "SOURCE_ID": str(row.get("sourceId") or ""),
            "ASSIGNED_BY_ID": GetCatalogService._stringify_value(row.get("assignedById")),
            "CATEGORY_ID": GetCatalogService._stringify_value(row.get("categoryId")),
            "DATE_CREATE": str(row.get("createdTime") or ""),
            "DATE_MODIFY": str(row.get("updatedTime") or ""),
        }

    @staticmethod
    def _coerce_numeric_id(value: str) -> int | str:
        text = value.strip()
        if text.isdigit():
            return int(text)
        return text

    @staticmethod
    def _stringify_value(value: Any) -> str:
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _collect_scope_dates(
        deals: list[dict[str, Any]],
        *,
        date_from: str | None,
        date_to: str | None,
    ) -> list[str]:
        dates: list[str] = []
        has_bounds = bool(date_from or date_to)
        for deal in deals:
            if has_bounds:
                for field in ("DATE_CREATE", "DATE_MODIFY"):
                    raw = str(deal.get(field) or "").strip()
                    if raw and within_datetime_range(raw, date_from=date_from, date_to=date_to):
                        dates.append(raw)
                continue

            dm = str(deal.get("DATE_MODIFY") or "").strip()
            if dm:
                dates.append(dm)
                continue

            dc = str(deal.get("DATE_CREATE") or "").strip()
            if dc:
                dates.append(dc)
        return dates

    def _build_manager_index(self) -> dict[str, dict[str, str]]:
        managers = self.get_managers()
        return {m["id"]: m for m in managers}

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
