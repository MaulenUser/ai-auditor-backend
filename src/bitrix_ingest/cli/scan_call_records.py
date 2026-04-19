"""CLI entry point for the call-records scan."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from ..application.call_records import CallRecordsScanRequest, CallRecordsScanService
from ..infrastructure.http import BitrixClient
from ..infrastructure.logging_config import setup_logging
from ..infrastructure.persistence import FileSystemJsonWriter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan Bitrix24 call records via VoxImplant statistics (ports bitrix-call-records-scan.ps1)"
    )
    parser.add_argument("--webhook-base-url", required=True, help="Bitrix24 webhook base URL")
    parser.add_argument(
        "--output-dir",
        default="export/call-records-scan",
        help="Output directory (default: export/call-records-scan)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max activities to process from the first page (default: 20)",
    )
    return parser


def run(
    webhook_base_url: str,
    output_dir: str = "export/call-records-scan",
    limit: int = 20,
) -> None:
    service = CallRecordsScanService(
        gateway=BitrixClient(webhook_base_url),
        sink=FileSystemJsonWriter(),
    )
    service.execute(
        CallRecordsScanRequest(
            output_dir=Path(output_dir),
            limit=limit,
        )
    )


def main(argv: Sequence[str] | None = None) -> None:
    setup_logging()
    args = build_parser().parse_args(argv)
    run(
        webhook_base_url=args.webhook_base_url,
        output_dir=args.output_dir,
        limit=args.limit,
    )


__all__ = ["build_parser", "main", "run"]
