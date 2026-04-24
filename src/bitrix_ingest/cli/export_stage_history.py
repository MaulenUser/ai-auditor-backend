"""CLI entry point for deal stage history export."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from ..application.crm import StageHistoryRequest, StageHistoryService
from ..infrastructure.http import BitrixClient
from ..infrastructure.logging_config import setup_logging
from ..infrastructure.persistence import FileSystemJsonWriter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export deal stage transition history via crm.stagehistory.list"
    )
    parser.add_argument(
        "--webhook-base-url", required=True,
        help="Bitrix24 webhook base URL",
    )
    parser.add_argument(
        "--output-dir", default="export/stage-history",
        help="Output directory (default: export/stage-history)",
    )
    parser.add_argument(
        "--category-id", nargs="+", dest="category_ids",
        help="Funnel (category) IDs to export (default: all)",
    )
    parser.add_argument(
        "--deal-ids", nargs="+",
        help="Export only these specific deal IDs",
    )
    parser.add_argument("--date-from", help="Filter by DATE_CREATE/DATE_MODIFY >= (ISO 8601)")
    parser.add_argument("--date-to", help="Filter by DATE_CREATE/DATE_MODIFY <= (ISO 8601)")
    parser.add_argument("--responsible-id", help="Filter by ASSIGNED_BY_ID")
    parser.add_argument(
        "--whatsapp-only", action="store_true",
        help="Export only WhatsApp deals (SOURCE_ID starts with WZ or title contains whatsapp)",
    )
    parser.add_argument("--limit", type=int, default=0, help="Max deals (0 = all)")
    parser.add_argument("--skip-existing", action="store_true", help="Skip already-exported deals")
    parser.add_argument("--page-delay", type=float, default=0.0, help="Seconds between pages")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    setup_logging()
    args = build_parser().parse_args(argv)
    StageHistoryService(
        gateway=BitrixClient(args.webhook_base_url, page_delay=args.page_delay),
        sink=FileSystemJsonWriter(),
    ).execute(StageHistoryRequest(
        output_dir=Path(args.output_dir),
        category_ids=args.category_ids,
        deal_ids=args.deal_ids,
        date_from=args.date_from,
        date_to=args.date_to,
        responsible_id=args.responsible_id,
        whatsapp_only=args.whatsapp_only,
        limit=args.limit,
        skip_existing=args.skip_existing,
    ))


__all__ = ["build_parser", "main"]
