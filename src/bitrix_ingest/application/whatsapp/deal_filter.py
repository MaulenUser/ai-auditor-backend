"""WhatsApp deal filtering and sorting.

Isolates the detection rules so they're easy to change per-portal without
touching the exporter code.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


_WHATSAPP_TITLE = re.compile(r"whatsapp", re.IGNORECASE)


class WhatsAppDealFilter:
    """Picks WhatsApp deals from a pool and sorts them by last modification.

    A deal is considered WhatsApp when either:
      - ``SOURCE_ID`` starts with ``"WZ"`` (Wazzup prefix), or
      - ``TITLE`` contains ``"whatsapp"`` (case-insensitive).
    """

    def select_whatsapp_deals(self, deals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [d for d in deals if self._is_whatsapp(d)]

    def restrict_to_allowlist(
        self,
        deals: list[dict[str, Any]],
        allowlist: list[str] | None,
    ) -> list[dict[str, Any]]:
        if not allowlist:
            return deals
        allowed = {str(deal_id) for deal_id in allowlist}
        return [d for d in deals if str(d.get("ID")) in allowed]

    def sort_by_modified_desc(self, deals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(deals, key=self._sort_key, reverse=True)

    def apply_limit(self, deals: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return deals
        return deals[:limit]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _is_whatsapp(deal: dict[str, Any]) -> bool:
        source = str(deal.get("SOURCE_ID", ""))
        title = str(deal.get("TITLE", ""))
        return source.startswith("WZ") or bool(_WHATSAPP_TITLE.search(title))

    @staticmethod
    def _sort_key(deal: dict[str, Any]) -> tuple[datetime, int]:
        raw = deal.get("DATE_MODIFY")
        if raw:
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                return dt, int(deal.get("ID") or 0)
            except ValueError:
                pass
        return datetime.min.replace(tzinfo=timezone.utc), int(deal.get("ID") or 0)
