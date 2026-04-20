"""CLI entry point for WhatsApp timeline-comment export."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from ..application.whatsapp_timeline import (
    WhatsAppTimelineExportRequest,
    WhatsAppTimelineExportService,
)
from ..infrastructure.http import BitrixClient
from ..infrastructure.logging_config import setup_logging
from ..infrastructure.persistence import FileSystemJsonWriter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export WhatsApp conversations via timeline comments "
        "(port of bitrix-export-whatsapp-timeline.ps1)"
    )
    parser.add_argument(
        "--webhook-base-url", required=True,
        help="Bitrix24 webhook base URL",
    )
    parser.add_argument(
        "--output-dir", default="export/whatsapp-timeline",
        help="Output directory (default: export/whatsapp-timeline)",
    )
    parser.add_argument("--limit", type=int, default=100, help="Max deals (default: 100)")
    parser.add_argument("--date-from", help="Filter deals modified on or after this date (ISO 8601)")
    parser.add_argument("--date-to", help="Filter deals modified on or before this date (ISO 8601)")
    parser.add_argument(
        "--deal-ids", nargs="+",
        help="Restrict export to these deal IDs",
    )
    parser.add_argument("--skip-existing", action="store_true", help="Skip already-exported deals")
    parser.add_argument("--page-delay", type=float, default=0.0, help="Seconds between pages")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    setup_logging()
    args = build_parser().parse_args(argv)
    WhatsAppTimelineExportService(
        gateway=BitrixClient(args.webhook_base_url, page_delay=args.page_delay),
        sink=FileSystemJsonWriter(),
    ).execute(WhatsAppTimelineExportRequest(
        output_dir=Path(args.output_dir),
        limit=args.limit,
        date_from=args.date_from,
        date_to=args.date_to,
        deal_ids=args.deal_ids,
        skip_existing=args.skip_existing,
    ))


__all__ = ["build_parser", "main"]
