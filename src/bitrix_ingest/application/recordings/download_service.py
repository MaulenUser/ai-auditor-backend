"""DownloadRecordingsService — fetches audio files listed in recording-candidates.json."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ...domain.recordings import (
    RecordingCandidate,
    RecordingDownloadError,
    RecordingManifestEntry,
)
from ..ports import FileDownloader, JsonSink
from ..progress import ProgressCallback, emit_progress

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadRecordingsRequest:
    source_json_path: Path
    output_dir: Path
    skip_existing: bool = False
    progress_callback: ProgressCallback | None = None


class DownloadRecordingsService:
    def __init__(self, downloader: FileDownloader, sink: JsonSink) -> None:
        self._downloader = downloader
        self._sink = sink

    def execute(self, request: DownloadRecordingsRequest) -> None:
        source = request.source_json_path
        if not source.exists():
            raise FileNotFoundError(f"Source JSON file not found: {source}")

        candidates = self._load_candidates(source)
        if not candidates:
            raise ValueError(f"No recording candidates found in {source}")

        output_dir = request.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        manifest: list[RecordingManifestEntry] = []
        errors: list[RecordingDownloadError] = []

        total = len(candidates)
        emit_progress(
            request.progress_callback,
            current=0,
            total=total,
            message=f"Найдено записей для скачивания: {total}",
        )
        for index, candidate in enumerate(candidates, start=1):
            self._process(candidate, output_dir, request.skip_existing, manifest, errors)
            emit_progress(
                request.progress_callback,
                current=index,
                total=total,
                message=f"Скачиваем записи звонков: {index} из {total}",
            )

        self._sink.write(output_dir / "manifest.json", [e.to_dict() for e in manifest])
        self._sink.write(output_dir / "download-errors.json", [e.to_dict() for e in errors])

        logger.info("Download completed.")
        logger.info("Manifest entries: %d", len(manifest))
        logger.info("Errors: %d", len(errors))
        logger.info("Files saved to %s", output_dir.resolve())

    @staticmethod
    def _load_candidates(path: Path) -> list[RecordingCandidate]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        # PS unwraps [[...]] when the root is a single-element array-of-arrays
        if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], list):
            raw = raw[0]
        if not isinstance(raw, list):
            raw = [raw]
        return [RecordingCandidate.from_dict(item) for item in raw if isinstance(item, dict)]

    def _process(
        self,
        candidate: RecordingCandidate,
        output_dir: Path,
        skip_existing: bool,
        manifest: list[RecordingManifestEntry],
        errors: list[RecordingDownloadError],
    ) -> None:
        url = candidate.call_record_url
        activity_id = candidate.crm_activity_id

        if not url or not url.strip():
            errors.append(RecordingDownloadError(
                crm_activity_id=activity_id,
                call_id=candidate.call_id,
                error="CALL_RECORD_URL is empty",
                url="",
            ))
            return

        target = output_dir / candidate.build_filename()

        if skip_existing and target.exists():
            manifest.append(RecordingManifestEntry(
                crm_activity_id=activity_id,
                call_id=candidate.call_id,
                file_path=str(target),
                status="skipped_existing",
                phone_number=candidate.phone_number,
                start_time=candidate.start_time,
                record_file_id=candidate.record_file_id,
                record_duration=candidate.record_duration,
            ))
            logger.info("Skipped existing file for CRM_ACTIVITY_ID=%s", activity_id)
            return

        try:
            self._downloader.download(url, target)
            manifest.append(RecordingManifestEntry(
                crm_activity_id=activity_id,
                call_id=candidate.call_id,
                file_path=str(target),
                status="downloaded",
                phone_number=candidate.phone_number,
                start_time=candidate.start_time,
                record_file_id=candidate.record_file_id,
                record_duration=candidate.record_duration,
            ))
            logger.info("Downloaded CRM_ACTIVITY_ID=%s", activity_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(RecordingDownloadError(
                crm_activity_id=activity_id,
                call_id=candidate.call_id,
                error=str(exc),
                url=url,
            ))
            logger.warning("Failed to download CRM_ACTIVITY_ID=%s: %s", activity_id, exc)
