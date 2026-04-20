"""Transcript domain entities — mirrors the PS1 manifest/error output shapes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TranscriptManifestEntry:
    crm_activity_id: str
    record_file_id: str
    audio_file_path: str
    text_file_path: str
    raw_file_path: str
    status: str
    model: str
    language: str
    text_length: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    audio_tokens: int = 0
    text_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "CRM_ACTIVITY_ID": self.crm_activity_id,
            "RECORD_FILE_ID": self.record_file_id,
            "AUDIO_FILE_PATH": self.audio_file_path,
            "TEXT_FILE_PATH": self.text_file_path,
            "RAW_FILE_PATH": self.raw_file_path,
            "STATUS": self.status,
            "MODEL": self.model,
            "LANGUAGE": self.language,
            "TEXT_LENGTH": self.text_length,
            "INPUT_TOKENS": self.input_tokens,
            "OUTPUT_TOKENS": self.output_tokens,
            "TOTAL_TOKENS": self.total_tokens,
            "AUDIO_TOKENS": self.audio_tokens,
            "TEXT_TOKENS": self.text_tokens,
        }


@dataclass(frozen=True)
class TranscriptError:
    crm_activity_id: str
    record_file_id: str
    source_file: str
    error: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "CRM_ACTIVITY_ID": self.crm_activity_id,
            "RECORD_FILE_ID": self.record_file_id,
            "SOURCE_FILE": self.source_file,
            "Error": self.error,
        }
