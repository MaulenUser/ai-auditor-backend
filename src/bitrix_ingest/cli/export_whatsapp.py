"""CLI entry point for the WhatsApp Open Lines export."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from ..application.whatsapp import WhatsAppExportRequest, WhatsAppExportService
from ..infrastructure.http import BitrixClient
from ..infrastructure.logging_config import setup_logging
from ..infrastructure.persistence import FileSystemJsonWriter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export WhatsApp Open Lines conversations from Bitrix24 "
            "(requires crm + imopenlines webhook scopes)"
        )
    )
    parser.add_argument("--webhook-base-url", required=True, help="Bitrix24 webhook base URL")
    parser.add_argument(
        "--output-dir",
        default="export/whatsapp-timeline",
        help="Output directory (default: export/whatsapp-timeline)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max WhatsApp deals to process after filtering and sort (default: 100; 0=no limit)",
    )
    parser.add_argument(
        "--date-from",
        "--modified-from",
        dest="date_from",
        default=None,
        help="Inclusive start date/datetime filter on deal DATE_MODIFY and message timestamps",
    )
    parser.add_argument(
        "--date-to",
        dest="date_to",
        default=None,
        help="Inclusive end date/datetime filter on deal DATE_MODIFY and message timestamps",
    )
    parser.add_argument(
        "--deal-ids",
        nargs="*",
        default=None,
        help="Optional explicit deal ID allowlist (applied after WhatsApp filter)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip deals with existing conversation files",
    )
    parser.add_argument(
        "--exclude-system-messages",
        action="store_true",
        help="Exclude Open Lines system events from exported messages",
    )
    parser.add_argument(
        "--page-delay",
        type=float,
        default=0.0,
        help="Seconds to sleep between pagination requests (default: 0)",
    )
    return parser


def run(
    webhook_base_url: str,
    output_dir: str = "export/whatsapp-timeline",
    limit: int = 100,
    date_from: str | None = None,
    date_to: str | None = None,
    deal_ids: list[str] | None = None,
    skip_existing: bool = False,
    exclude_system_messages: bool = False,
    page_delay: float = 0.0,
) -> None:
    service = WhatsAppExportService(
        gateway=BitrixClient(webhook_base_url, page_delay=page_delay),
        sink=FileSystemJsonWriter(),
    )
    service.execute(
        WhatsAppExportRequest(
            output_dir=Path(output_dir),
            limit=limit,
            date_from=date_from,
            date_to=date_to,
            deal_ids=deal_ids,
            skip_existing=skip_existing,
            include_system_messages=not exclude_system_messages,
        )
    )


def main(argv: Sequence[str] | None = None) -> None:
    setup_logging()
    args = build_parser().parse_args(argv)
    run(
        webhook_base_url=args.webhook_base_url,
        output_dir=args.output_dir,
        limit=args.limit,
        date_from=args.date_from,
        date_to=args.date_to,
        deal_ids=args.deal_ids,
        skip_existing=args.skip_existing,
        exclude_system_messages=args.exclude_system_messages,
        page_delay=args.page_delay,
    )


__all__ = ["build_parser", "main", "run"]
