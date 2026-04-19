"""CrmExportService — orchestrates a full CRM base-snapshot export."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..date_range import build_closed_filter
from ..ports import BitrixGateway, JsonSink
from .specs import ACTIVITY_SPEC, CRM_ENTITY_SPECS, EntityExportSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CrmExportRequest:
    """All inputs the export service needs to do its job.

    Using a request object instead of a long ``run()`` signature keeps the
    service easy to extend (add a field → nothing else changes) and clarifies
    at call-sites what is being requested.
    """

    output_dir: Path
    date_from: str | None = None
    date_to: str | None = None
    skip_users: bool = False
    skip_activities: bool = False
    limit: int | None = None


class CrmExportService:
    """Export profile, users, and CRM list entities to JSON files.

    The service depends on the :class:`BitrixGateway` and :class:`JsonSink`
    Protocols — not on HTTP or filesystem concretes. It can therefore be
    driven by any gateway/sink pair (real or fake) without modification.
    """

    def __init__(self, gateway: BitrixGateway, sink: JsonSink) -> None:
        self._gateway = gateway
        self._sink = sink

    def execute(self, request: CrmExportRequest) -> None:
        output_dir = request.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        self._export_profile(output_dir)
        if not request.skip_users:
            self._export_users(output_dir)

        date_filter = build_closed_filter(
            "DATE_MODIFY",
            date_from=request.date_from,
            date_to=request.date_to,
        )
        for spec in CRM_ENTITY_SPECS:
            self._export_entity(spec, output_dir, date_filter, request.limit)

        if not request.skip_activities:
            activity_filter = build_closed_filter(
                ACTIVITY_SPEC.modified_field,
                date_from=request.date_from,
                date_to=request.date_to,
            )
            self._export_entity(ACTIVITY_SPEC, output_dir, activity_filter, request.limit)

        logger.info("Export completed. Files saved to %s", output_dir.resolve())

    # ------------------------------------------------------------------
    # Internal steps
    # ------------------------------------------------------------------

    def _export_profile(self, output_dir: Path) -> None:
        profile = self._gateway.call("profile")
        result = profile.get("result") or {}
        logger.info(
            "Connected as user ID %s: %s %s",
            result.get("ID"),
            result.get("NAME"),
            result.get("LAST_NAME"),
        )
        self._sink.write(output_dir / "profile.json", profile)

    def _export_users(self, output_dir: Path) -> None:
        users = self._gateway.call("user.get")
        self._sink.write(output_dir / "users.json", users)
        user_list = users.get("result") or []
        count = len(user_list) if isinstance(user_list, list) else 1
        logger.info("Users exported: %d", count)

    def _export_entity(
        self,
        spec: EntityExportSpec,
        output_dir: Path,
        filter_: dict[str, str],
        limit: int | None = None,
    ) -> None:
        rows = self._gateway.list_all(
            spec.method,
            select=list(spec.select),
            filter=filter_,
            limit=limit,
        )
        if limit is not None:
            rows = rows[:limit]
        self._sink.write(output_dir / spec.output_file, rows)
        logger.info("%s exported: %d", spec.name, len(rows))
