"""Call-records scan domain entities."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CallScanRow:
    """One enriched row in the call-records scan output."""

    crm_activity_id: str
    subject: str
    start_time: str
    responsible_id: str
    owner_id: str
    owner_type_id: str
    phone_number: str
    call_id: str
    call_duration: int
    record_duration: int
    call_record_url: str
    record_file_id: str
    transcript_id: str
    call_failed_code: str
    call_failed_reason: str
    has_recording: bool

    @property
    def is_successful(self) -> bool:
        """A call is successful when it has a duration and no failure code."""
        return self.call_duration > 0 and not self.call_failed_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "CRM_ACTIVITY_ID": self.crm_activity_id,
            "SUBJECT": self.subject,
            "START_TIME": self.start_time,
            "RESPONSIBLE_ID": self.responsible_id,
            "OWNER_ID": self.owner_id,
            "OWNER_TYPE_ID": self.owner_type_id,
            "PHONE_NUMBER": self.phone_number,
            "CALL_ID": self.call_id,
            "CALL_DURATION": self.call_duration,
            "RECORD_DURATION": self.record_duration,
            "CALL_RECORD_URL": self.call_record_url,
            "RECORD_FILE_ID": self.record_file_id,
            "TRANSCRIPT_ID": self.transcript_id,
            "CALL_FAILED_CODE": self.call_failed_code,
            "CALL_FAILED_REASON": self.call_failed_reason,
            "HAS_RECORDING": self.has_recording,
        }


@dataclass
class CallScanError:
    """An activity that could not be enriched; captured for the error report."""

    crm_activity_id: str
    subject: str
    error: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "CRM_ACTIVITY_ID": self.crm_activity_id,
            "SUBJECT": self.subject,
            "Error": self.error,
        }
