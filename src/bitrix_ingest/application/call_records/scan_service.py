"""CallRecordsScanService — enriches Bitrix call activities with VoxImplant stats."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.call_records import CallScanError, CallScanRow
from ..date_range import build_closed_filter
from ..ports import BitrixGateway, JsonSink
from .coercion import coerce_int, coerce_str

logger = logging.getLogger(__name__)


_ACTIVITY_FILTER: dict[str, Any] = {
    "TYPE_ID": "2",
    "PROVIDER_ID": "VOXIMPLANT_CALL",
    "PROVIDER_TYPE_ID": "CALL",
}
_ACTIVITY_SELECT: list[str] = [
    "ID", "OWNER_ID", "OWNER_TYPE_ID", "RESPONSIBLE_ID", "SUBJECT",
    "START_TIME", "END_TIME", "DIRECTION", "PROVIDER_ID", "PROVIDER_TYPE_ID",
    "COMPLETED", "LAST_UPDATED",
]


@dataclass(frozen=True)
class CallRecordsScanRequest:
    output_dir: Path
    limit: int = 20
    date_from: str | None = None
    date_to: str | None = None
    responsible_id: str | None = None


@dataclass
class _ScanResult:
    """Internal bucket for in-flight scan state. Not part of the public API."""

    rows: list[CallScanRow]
    raw_stats: list[dict[str, Any]]
    errors: list[CallScanError]

    @classmethod
    def empty(cls) -> "_ScanResult":
        return cls(rows=[], raw_stats=[], errors=[])


class CallRecordsScanService:
    """Fetches recent call activities and enriches each with VoxImplant stats.

    BEHAVIOURAL NOTE (preserved from the PS port — see MIGRATION.md §call-scan-limit):
    The activity fetch is intentionally single-page. Upgrade path documented there.
    """

    def __init__(self, gateway: BitrixGateway, sink: JsonSink) -> None:
        self._gateway = gateway
        self._sink = sink

    def execute(self, request: CallRecordsScanRequest) -> None:
        output_dir = request.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        activities = self._fetch_activities(
            limit=request.limit,
            date_from=request.date_from,
            date_to=request.date_to,
            responsible_id=request.responsible_id,
        )
        self._sink.write(output_dir / "activities.source.json", activities)

        result = _ScanResult.empty()
        for activity in activities:
            self._scan_activity(activity, into=result)

        self._write_outputs(output_dir, result)
        self._log_summary(output_dir, result)

    # ------------------------------------------------------------------
    # Fetching / enrichment
    # ------------------------------------------------------------------

    def _fetch_activities(
        self,
        *,
        limit: int,
        date_from: str | None,
        date_to: str | None,
        responsible_id: str | None = None,
    ) -> list[dict[str, Any]]:
        filter_ = dict(_ACTIVITY_FILTER)
        filter_.update(
            build_closed_filter(
                "START_TIME",
                date_from=date_from,
                date_to=date_to,
            )
        )
        if responsible_id:
            filter_["RESPONSIBLE_ID"] = responsible_id
        response = self._gateway.call(
            "crm.activity.list",
            body={
                "filter": filter_,
                "order": {"START_TIME": "DESC"},
                "select": _ACTIVITY_SELECT,
                "start": 0,
            },
            label="crm.activity.list page 1 (start=0)",
        )
        raw_result: list[dict[str, Any]] = response.get("result") or []
        activities = raw_result[:limit] if limit > 0 else raw_result
        logger.info(
            "Loaded call activities: %d (page returned %d, limit=%d)",
            len(activities),
            len(raw_result),
            limit,
        )
        return activities

    def _scan_activity(self, activity: dict[str, Any], *, into: _ScanResult) -> None:
        activity_id = coerce_str(activity.get("ID"))
        try:
            stat_response = self._gateway.call(
                "voximplant.statistic.get",
                body={
                    "filter": {"CRM_ACTIVITY_ID": activity_id},
                    "SORT": "CALL_START_DATE",
                    "ORDER": "DESC",
                },
                label=f"voximplant.statistic.get CRM_ACTIVITY_ID={activity_id}",
            )
        except Exception as exc:  # noqa: BLE001 — we record every failure mode
            into.errors.append(
                CallScanError(
                    crm_activity_id=activity_id,
                    subject=coerce_str(activity.get("SUBJECT")),
                    error=str(exc),
                )
            )
            logger.warning("Skipped CRM_ACTIVITY_ID=%s due to error: %s", activity_id, exc)
            return

        into.raw_stats.append({"CRM_ACTIVITY_ID": activity_id, "Response": stat_response})
        stat_items: list[dict[str, Any]] = stat_response.get("result") or []

        if not stat_items:
            into.rows.append(self._empty_row(activity, activity_id))
            logger.info("Checked CRM_ACTIVITY_ID=%s (no stats)", activity_id)
            return

        for stat in stat_items:
            into.rows.append(self._row_from_stat(activity, activity_id, stat))
        logger.info("Checked CRM_ACTIVITY_ID=%s", activity_id)

    @staticmethod
    def _empty_row(activity: dict[str, Any], activity_id: str) -> CallScanRow:
        return CallScanRow(
            crm_activity_id=activity_id,
            subject=coerce_str(activity.get("SUBJECT")),
            start_time=coerce_str(activity.get("START_TIME")),
            responsible_id=coerce_str(activity.get("RESPONSIBLE_ID")),
            phone_number="",
            call_id="",
            call_duration=0,
            record_duration=0,
            call_record_url="",
            record_file_id="",
            transcript_id="",
            call_failed_code="",
            call_failed_reason="",
            has_recording=False,
        )

    @staticmethod
    def _row_from_stat(
        activity: dict[str, Any],
        activity_id: str,
        stat: dict[str, Any],
    ) -> CallScanRow:
        call_record_url = coerce_str(stat.get("CALL_RECORD_URL"))
        record_file_id = coerce_str(stat.get("RECORD_FILE_ID"))
        return CallScanRow(
            crm_activity_id=activity_id,
            subject=coerce_str(activity.get("SUBJECT")),
            start_time=coerce_str(activity.get("START_TIME")),
            responsible_id=coerce_str(activity.get("RESPONSIBLE_ID")),
            phone_number=coerce_str(stat.get("PHONE_NUMBER")),
            call_id=coerce_str(stat.get("CALL_ID")),
            call_duration=coerce_int(stat.get("CALL_DURATION")),
            record_duration=coerce_int(stat.get("RECORD_DURATION")),
            call_record_url=call_record_url,
            record_file_id=record_file_id,
            transcript_id=coerce_str(stat.get("TRANSCRIPT_ID")),
            call_failed_code=coerce_str(stat.get("CALL_FAILED_CODE")),
            call_failed_reason=coerce_str(stat.get("CALL_FAILED_REASON")),
            has_recording=bool(call_record_url or record_file_id),
        )

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _write_outputs(self, output_dir: Path, result: _ScanResult) -> None:
        with_recording = [r for r in result.rows if r.has_recording]
        successful = [r for r in result.rows if r.is_successful]

        self._sink.write(output_dir / "voximplant.raw.json", result.raw_stats)
        self._sink.write(output_dir / "scan-results.json", [r.to_dict() for r in result.rows])
        self._sink.write(
            output_dir / "recording-candidates.json",
            [r.to_dict() for r in with_recording],
        )
        self._sink.write(
            output_dir / "successful-calls.json",
            [r.to_dict() for r in successful],
        )
        self._sink.write(
            output_dir / "scan-errors.json",
            [e.to_dict() for e in result.errors],
        )

    def _log_summary(self, output_dir: Path, result: _ScanResult) -> None:
        with_recording = sum(1 for r in result.rows if r.has_recording)
        successful = sum(1 for r in result.rows if r.is_successful)
        logger.info("Scan completed.")
        logger.info("Rows total: %d", len(result.rows))
        logger.info("Calls with recording: %d", with_recording)
        logger.info("Successful calls without failure code: %d", successful)
        logger.info("Errors skipped: %d", len(result.errors))
        logger.info("Files saved to %s", output_dir.resolve())
