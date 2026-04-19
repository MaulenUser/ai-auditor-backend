"""CLI entry point for the CRM export."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from ..application.crm import CrmExportRequest, CrmExportService
from ..infrastructure.http import BitrixClient
from ..infrastructure.logging_config import setup_logging
from ..infrastructure.persistence import FileSystemJsonWriter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export base CRM snapshot from Bitrix24 (ports bitrix-export.ps1)"
    )
    parser.add_argument("--webhook-base-url", required=True, help="Bitrix24 webhook base URL")
    parser.add_argument("--output-dir", default="export", help="Output directory (default: export)")
    parser.add_argument("--modified-from", default=None, help="ISO date filter, e.g. 2024-01-01")
    parser.add_argument("--skip-users", action="store_true", help="Skip user.get export")
    parser.add_argument("--skip-activities", action="store_true", help="Skip crm.activity.list export")
    parser.add_argument("--limit", type=int, default=None, help="Max rows per entity (client-side, default: no limit)")
    return parser


def run(
    webhook_base_url: str,
    output_dir: str = "export",
    modified_from: str | None = None,
    skip_users: bool = False,
    skip_activities: bool = False,
    limit: int | None = None,
) -> None:
    service = CrmExportService(
        gateway=BitrixClient(webhook_base_url),
        sink=FileSystemJsonWriter(),
    )
    service.execute(
        CrmExportRequest(
            output_dir=Path(output_dir),
            modified_from=modified_from,
            skip_users=skip_users,
            skip_activities=skip_activities,
            limit=limit,
        )
    )


def main(argv: Sequence[str] | None = None) -> None:
    setup_logging()
    args = build_parser().parse_args(argv)
    run(
        webhook_base_url=args.webhook_base_url,
        output_dir=args.output_dir,
        modified_from=args.modified_from,
        skip_users=args.skip_users,
        skip_activities=args.skip_activities,
        limit=args.limit,
    )


__all__ = ["build_parser", "main", "run"]
