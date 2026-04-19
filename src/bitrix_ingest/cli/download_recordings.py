"""CLI entry point for downloading call recordings."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from ..application.recordings import DownloadRecordingsRequest, DownloadRecordingsService
from ..infrastructure.http.file_downloader import RequestsFileDownloader
from ..infrastructure.logging_config import setup_logging
from ..infrastructure.persistence import FileSystemJsonWriter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download call recordings listed in recording-candidates.json (ports download-call-recordings.ps1)"
    )
    parser.add_argument(
        "--source-json",
        default="export/call-records-scan/recording-candidates.json",
        help="Path to recording-candidates.json (default: export/call-records-scan/recording-candidates.json)",
    )
    parser.add_argument(
        "--output-dir",
        default="export/recordings",
        help="Directory to save recordings (default: export/recordings)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip files that already exist on disk",
    )
    return parser


def run(
    source_json: str = "export/call-records-scan/recording-candidates.json",
    output_dir: str = "export/recordings",
    skip_existing: bool = False,
) -> None:
    service = DownloadRecordingsService(
        downloader=RequestsFileDownloader(),
        sink=FileSystemJsonWriter(),
    )
    service.execute(DownloadRecordingsRequest(
        source_json_path=Path(source_json),
        output_dir=Path(output_dir),
        skip_existing=skip_existing,
    ))


def main(argv: Sequence[str] | None = None) -> None:
    setup_logging()
    args = build_parser().parse_args(argv)
    run(
        source_json=args.source_json,
        output_dir=args.output_dir,
        skip_existing=args.skip_existing,
    )


__all__ = ["build_parser", "main", "run"]
