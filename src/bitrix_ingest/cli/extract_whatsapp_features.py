"""CLI entry point for extracting WhatsApp chat features via OpenAI."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from ..application.whatsapp_features import (
    ExtractWhatsAppFeaturesRequest,
    ExtractWhatsAppFeaturesService,
)
from ..infrastructure.logging_config import setup_logging
from ..infrastructure.openai import OpenAiResponsesClient
from ..infrastructure.persistence import FileSystemJsonWriter


def _resolve_api_key(explicit: str | None) -> str:
    key = explicit or os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise SystemExit(
            "OpenAI API key not provided. Use --api-key or set OPENAI_API_KEY."
        )
    return key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract structured features from WhatsApp conversations via OpenAI "
        "(port of extract-whatsapp-features-openai.ps1)"
    )
    parser.add_argument(
        "--conversation-report",
        default="export/whatsapp-timeline/report.json",
        help="Path to WhatsApp export report.json (default: export/whatsapp-timeline/report.json)",
    )
    parser.add_argument(
        "--conversation-dir",
        default="export/whatsapp-timeline/conversations",
        help="Fallback directory of conversation JSON files",
    )
    parser.add_argument(
        "--output-dir", default="export/whatsapp-features",
        help="Output directory (default: export/whatsapp-features)",
    )
    parser.add_argument("--api-key", help="OpenAI API key (fallback: OPENAI_API_KEY env var)")
    parser.add_argument("--model", default="gpt-4o-mini", help="OpenAI model name")
    parser.add_argument("--limit", type=int, default=0, help="Max entries (0 = all)")
    parser.add_argument("--skip-existing", action="store_true", help="Skip already-extracted entries")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    setup_logging()
    args = build_parser().parse_args(argv)
    api_key = _resolve_api_key(args.api_key)
    ExtractWhatsAppFeaturesService(
        gateway=OpenAiResponsesClient(api_key),
        sink=FileSystemJsonWriter(),
    ).execute(ExtractWhatsAppFeaturesRequest(
        output_dir=Path(args.output_dir),
        conversation_report_path=Path(args.conversation_report),
        conversation_dir=Path(args.conversation_dir),
        model=args.model,
        limit=args.limit,
        skip_existing=args.skip_existing,
    ))


__all__ = ["build_parser", "main"]
