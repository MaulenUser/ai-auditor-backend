"""CLI entry point for the sales-quality analyzer."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from ..application.sales_quality import (
    AnalyzeSalesQualityRequest,
    AnalyzeSalesQualityService,
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


def _path_or_none(value: str | None) -> Path | None:
    if not value or value.strip().lower() in {"none", "null", "-"}:
        return None
    return Path(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze sales-quality problems and stage compliance across calls and WhatsApp"
    )
    parser.add_argument(
        "--call-transcript-manifest",
        default="export/recordings/transcripts/transcripts_manifest.json",
        help="Path to call transcripts manifest; use '-' to skip calls",
    )
    parser.add_argument(
        "--call-metadata",
        default="export/call-records-scan/recording-candidates.json",
        help="Path to recording-candidates.json; use '-' if unavailable",
    )
    parser.add_argument(
        "--activity-metadata",
        default="export/call-records-scan/activities.source.json",
        help="Path to call activities.source.json; use '-' if unavailable",
    )
    parser.add_argument(
        "--whatsapp-conversation-dir",
        default="export/whatsapp-timeline/conversations_filtered",
        help="Directory with filtered WhatsApp conversation JSON files; use '-' to skip WhatsApp",
    )
    parser.add_argument(
        "--users-path",
        default="",
        help="Optional users.json for manager names",
    )
    parser.add_argument(
        "--output-dir",
        default="export/sales-quality",
        help="Output directory (default: export/sales-quality)",
    )
    parser.add_argument("--api-key", help="OpenAI API key (fallback: OPENAI_API_KEY env var)")
    parser.add_argument("--model", default="gpt-4o-mini", help="OpenAI model name")
    parser.add_argument("--limit", type=int, default=0, help="Max interactions (0 = all)")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip interactions with existing feature and raw files",
    )
    parser.add_argument(
        "--slow-response-threshold-sec",
        type=int,
        default=900,
        help="Threshold for slow WhatsApp first response (default: 900)",
    )
    parser.add_argument(
        "--max-chars-per-item",
        type=int,
        default=24000,
        help="Max text chars sent per interaction (default: 24000)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    setup_logging()
    args = build_parser().parse_args(argv)
    api_key = _resolve_api_key(args.api_key)
    AnalyzeSalesQualityService(
        gateway=OpenAiResponsesClient(api_key),
        sink=FileSystemJsonWriter(),
    ).execute(
        AnalyzeSalesQualityRequest(
            output_dir=Path(args.output_dir),
            call_transcript_manifest_path=_path_or_none(args.call_transcript_manifest),
            call_metadata_path=_path_or_none(args.call_metadata),
            activity_metadata_path=_path_or_none(args.activity_metadata),
            whatsapp_conversation_dir=_path_or_none(args.whatsapp_conversation_dir),
            users_path=_path_or_none(args.users_path),
            model=args.model,
            limit=args.limit,
            skip_existing=args.skip_existing,
            slow_response_threshold_sec=args.slow_response_threshold_sec,
            max_chars_per_item=args.max_chars_per_item,
        )
    )


__all__ = ["build_parser", "main"]
