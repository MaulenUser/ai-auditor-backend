"""CLI entry point for generating dept-level recommendations from aggregate stats."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from ..application.analytics import GenerateRecommendationsRequest, GenerateRecommendationsService
from ..infrastructure.logging_config import setup_logging
from ..infrastructure.openai import OpenAiResponsesClient
from ..infrastructure.persistence import FileSystemJsonWriter


def _resolve_api_key(explicit: str | None) -> str:
    key = explicit or os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise SystemExit("OpenAI API key not provided. Use --api-key or set OPENAI_API_KEY.")
    return key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate department-level sales recommendations from aggregate statistics"
    )
    parser.add_argument(
        "--aggregate-path",
        default="export/call-analytics/aggregate.json",
        help="Path to aggregate.json (default: export/call-analytics/aggregate.json)",
    )
    parser.add_argument(
        "--output-dir",
        default="export/call-analytics",
        help="Output directory (default: export/call-analytics)",
    )
    parser.add_argument("--api-key", help="OpenAI API key (fallback: OPENAI_API_KEY env var)")
    parser.add_argument("--model", default="gpt-4o", help="OpenAI model (default: gpt-4o)")
    parser.add_argument("--source-label", default="", help="Label shown in the output")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    setup_logging()
    args = build_parser().parse_args(argv)
    api_key = _resolve_api_key(args.api_key)
    GenerateRecommendationsService(
        gateway=OpenAiResponsesClient(api_key),
        sink=FileSystemJsonWriter(),
    ).execute(
        GenerateRecommendationsRequest(
            aggregate_path=Path(args.aggregate_path),
            output_dir=Path(args.output_dir),
            model=args.model,
            source_label=args.source_label,
        )
    )


__all__ = ["build_parser", "main"]
