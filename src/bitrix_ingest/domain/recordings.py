"""Call-recording download domain entities and pure filename helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _safe_file_part(value: str) -> str:
    """Return a filesystem-safe token from *value*, mirroring Get-SafeFileNamePart."""
    if not value or not value.strip():
        return "unknown"
    safe = re.sub(r"\s+", "_", value)
    safe = re.sub(r"[^A-Za-z0-9_\-\+]", "_", safe)
    safe = safe.strip("_")
    return safe or "unknown"


def _ext_from_url(url: str) -> str:
    """Return the file extension from *url*'s path component, defaulting to .mp3."""
    if not url or not url.strip():
        return ".mp3"
    try:
        ext = Path(urlparse(url).path).suffix
        return ext if ext else ".mp3"
    except Exception:
        return ".mp3"


@dataclass(frozen=True)
class RecordingCandidate:
    """One row from recording-candidates.json."""

    crm_activity_id: str
    call_id: str
    call_record_url: str
    phone_number: str
    start_time: str
    record_file_id: str
    record_duration: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RecordingCandidate":
        return cls(
            crm_activity_id=str(raw.get("CRM_ACTIVITY_ID") or ""),
            call_id=str(raw.get("CALL_ID") or ""),
            call_record_url=str(raw.get("CALL_RECORD_URL") or ""),
            phone_number=str(raw.get("PHONE_NUMBER") or ""),
            start_time=str(raw.get("START_TIME") or ""),
            record_file_id=str(raw.get("RECORD_FILE_ID") or ""),
            record_duration=str(raw.get("RECORD_DURATION") or ""),
        )

    def build_filename(self) -> str:
        activity_id = _safe_file_part(self.crm_activity_id)
        record_file_id = _safe_file_part(self.record_file_id)
        call_id = _safe_file_part(self.call_id)
        ext = _ext_from_url(self.call_record_url)
        if record_file_id != "unknown":
            return f"activity_{activity_id}__record_{record_file_id}{ext}"
        return f"activity_{activity_id}__call_{call_id}{ext}"


@dataclass(frozen=True)
class RecordingManifestEntry:
    crm_activity_id: str
    call_id: str
    file_path: str
    status: str  # "downloaded" | "skipped_existing"
    phone_number: str
    start_time: str
    record_file_id: str
    record_duration: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "CRM_ACTIVITY_ID": self.crm_activity_id,
            "CALL_ID": self.call_id,
            "FILE_PATH": self.file_path,
            "STATUS": self.status,
            "PHONE_NUMBER": self.phone_number,
            "START_TIME": self.start_time,
            "RECORD_FILE_ID": self.record_file_id,
            "RECORD_DURATION": self.record_duration,
        }


@dataclass(frozen=True)
class RecordingDownloadError:
    crm_activity_id: str
    call_id: str
    error: str
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "CRM_ACTIVITY_ID": self.crm_activity_id,
            "CALL_ID": self.call_id,
            "Error": self.error,
            "URL": self.url,
        }
