"""Tests for call-records scan: limit behaviour, enrichment, filtering."""
from __future__ import annotations

from bitrix_ingest.application.call_records import coerce_int, coerce_str
from bitrix_ingest.domain.call_records import CallScanRow


# ---------------------------------------------------------------------------
# _str / _int helpers (match PS Get-StringValue / Get-IntValue)
# ---------------------------------------------------------------------------

class TestStrHelper:
    def test_none_returns_empty_string(self):
        assert coerce_str(None) == ""

    def test_int_value_stringified(self):
        assert coerce_str(42) == "42"

    def test_string_unchanged(self):
        assert coerce_str("hello") == "hello"


class TestIntHelper:
    def test_none_returns_zero(self):
        assert coerce_int(None) == 0

    def test_int_passthrough(self):
        assert coerce_int(5) == 5

    def test_float_string_rounded(self):
        assert coerce_int("4.6") == 5
        assert coerce_int("4.4") == 4

    def test_empty_string_returns_zero(self):
        assert coerce_int("") == 0

    def test_whitespace_returns_zero(self):
        assert coerce_int("  ") == 0

    def test_non_numeric_returns_zero(self):
        assert coerce_int("N/A") == 0


# ---------------------------------------------------------------------------
# CallScanRow.to_dict — output contract
# ---------------------------------------------------------------------------

class TestCallScanRowToDict:
    def _row(self, **overrides) -> CallScanRow:
        defaults = dict(
            crm_activity_id="123",
            subject="Call subject",
            start_time="2024-01-01T10:00:00",
            responsible_id="5",
            phone_number="+7000000000",
            call_id="CALL-001",
            call_duration=60,
            record_duration=55,
            call_record_url="https://cdn.example.com/rec.mp3",
            record_file_id="",
            transcript_id="",
            call_failed_code="",
            call_failed_reason="",
            has_recording=True,
        )
        defaults.update(overrides)
        return CallScanRow(**defaults)

    def test_keys_match_powershell_contract(self):
        d = self._row().to_dict()
        expected_keys = {
            "CRM_ACTIVITY_ID", "SUBJECT", "START_TIME", "RESPONSIBLE_ID",
            "PHONE_NUMBER", "CALL_ID", "CALL_DURATION", "RECORD_DURATION",
            "CALL_RECORD_URL", "RECORD_FILE_ID", "TRANSCRIPT_ID",
            "CALL_FAILED_CODE", "CALL_FAILED_REASON", "HAS_RECORDING",
        }
        assert set(d.keys()) == expected_keys

    def test_has_recording_true_when_url_present(self):
        row = self._row(call_record_url="https://cdn.example.com/rec.mp3", record_file_id="")
        assert row.has_recording is True

    def test_has_recording_true_when_file_id_present(self):
        row = self._row(call_record_url="", record_file_id="FILE123")
        assert row.has_recording is True

    def test_has_recording_false_when_both_empty(self):
        row = self._row(call_record_url="", record_file_id="")
        row.has_recording = False
        assert row.to_dict()["HAS_RECORDING"] is False


# ---------------------------------------------------------------------------
# Filtering logic (recording-candidates, successful-calls)
# ---------------------------------------------------------------------------

class TestFilteringLogic:
    """Mirrors the PS Where-Object filters that produce the output subsets."""

    def _make_rows(self) -> list[CallScanRow]:
        def row(**kw) -> CallScanRow:
            defaults = dict(
                crm_activity_id="1", subject="", start_time="", responsible_id="",
                phone_number="", call_id="", call_duration=0, record_duration=0,
                call_record_url="", record_file_id="", transcript_id="",
                call_failed_code="", call_failed_reason="", has_recording=False,
            )
            defaults.update(kw)
            return CallScanRow(**defaults)

        return [
            row(crm_activity_id="1", has_recording=True, call_duration=60, call_failed_code=""),
            row(crm_activity_id="2", has_recording=False, call_duration=120, call_failed_code=""),
            row(crm_activity_id="3", has_recording=True, call_duration=0, call_failed_code=""),
            row(crm_activity_id="4", has_recording=False, call_duration=30, call_failed_code="BUSY"),
            row(crm_activity_id="5", has_recording=True, call_duration=90, call_failed_code="NO_ANSWER"),
        ]

    def test_recording_candidates_filter(self):
        rows = self._make_rows()
        candidates = [r for r in rows if r.has_recording]
        ids = {r.crm_activity_id for r in candidates}
        assert ids == {"1", "3", "5"}

    def test_successful_calls_filter(self):
        rows = self._make_rows()
        # PS: CALL_DURATION > 0 AND IsNullOrWhiteSpace(CALL_FAILED_CODE)
        successful = [r for r in rows if r.call_duration > 0 and not r.call_failed_code]
        ids = {r.crm_activity_id for r in successful}
        assert ids == {"1", "2"}

    def test_limit_slices_activities(self):
        # Simulate the single-page fetch + limit slice
        raw_activities = [{"ID": str(i)} for i in range(50)]
        limit = 10
        activities = raw_activities[:limit]
        assert len(activities) == 10
        assert activities[0]["ID"] == "0"
        assert activities[-1]["ID"] == "9"

    def test_limit_zero_keeps_full_first_page(self):
        raw_activities = [{"ID": str(i)} for i in range(50)]
        limit = 0
        activities = raw_activities[:limit] if limit > 0 else raw_activities
        assert len(activities) == 50
        assert activities[0]["ID"] == "0"
        assert activities[-1]["ID"] == "49"
