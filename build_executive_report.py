"""Build the final executive sales report from Bitrix and sales-quality output."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [part.strip() for part in value.split(",") if part.strip()]
    return items or None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build executive sales dashboard from Bitrix and sales-quality report"
    )
    parser.add_argument("--webhook-base-url", required=True)
    parser.add_argument("--sales-quality-dir", default="export/sales-quality")
    parser.add_argument("--output-dir", default="export/executive-report")
    parser.add_argument("--scope", choices=["analyzed", "bitrix"], default="analyzed")
    parser.add_argument("--date-from", default=None)
    parser.add_argument("--date-to", default=None)
    parser.add_argument("--category-ids", default=None)
    parser.add_argument("--responsible-id", default=None)
    parser.add_argument("--deal-ids", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--average-ticket-kzt", type=float, default=None)
    parser.add_argument("--expected-conversion-pct", type=float, default=None)
    parser.add_argument("--rating-formula", default="default")
    parser.add_argument("--portal-base-url", default="https://sapaplast.bitrix24.kz")
    parser.add_argument("--max-reanimation-cards", type=int, default=100)
    args = parser.parse_args()

    sys.path.insert(0, "src")
    from bitrix_ingest.application.executive_report import (
        BuildExecutiveReportRequest,
        BuildExecutiveReportService,
    )
    from bitrix_ingest.infrastructure.http import BitrixClient
    from bitrix_ingest.infrastructure.persistence import FileSystemJsonWriter

    BuildExecutiveReportService(
        gateway=BitrixClient(args.webhook_base_url),
        sink=FileSystemJsonWriter(),
    ).execute(
        BuildExecutiveReportRequest(
            output_dir=Path(args.output_dir),
            sales_quality_dir=Path(args.sales_quality_dir),
            scope=args.scope,
            date_from=args.date_from,
            date_to=args.date_to,
            category_ids=_split_csv(args.category_ids),
            responsible_id=args.responsible_id,
            deal_ids=_split_csv(args.deal_ids),
            limit=args.limit,
            average_ticket_kzt=args.average_ticket_kzt,
            expected_conversion_pct=args.expected_conversion_pct,
            rating_formula=args.rating_formula,
            portal_base_url=args.portal_base_url,
            max_reanimation_cards=args.max_reanimation_cards,
        )
    )


if __name__ == "__main__":
    main()
