"""CLI entry point for extracting call features via OpenAI."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from ..application.call_features import ExtractCallFeaturesRequest, ExtractCallFeaturesService
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
        description="Extract structured features from call transcripts via OpenAI "
        "(port of extract-call-features-openai.ps1)"
    )
    parser.add_argument(
        "--transcript-manifest",
        default="export/transcripts/manifest.json",
        help="Path to transcripts manifest.json (default: export/transcripts/manifest.json)",
    )
    parser.add_argument(
        "--call-metadata",
        default="export/call-records-scan/recording-candidates.json",
        help="Path to recording-candidates.json for call metadata",
    )
    parser.add_argument(
        "--activity-metadata",
        default="export/call-records-scan/activities.source.json",
        help="Path to activities.source.json for DIRECTION field",
    )
    parser.add_argument(
        "--output-dir", default="export/call-features",
        help="Output directory (default: export/call-features)",
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

    call_meta = Path(args.call_metadata) if args.call_metadata else None
    activity_meta = Path(args.activity_metadata) if args.activity_metadata else None

    ExtractCallFeaturesService(
        gateway=OpenAiResponsesClient(api_key),
        sink=FileSystemJsonWriter(),
    ).execute(ExtractCallFeaturesRequest(
        transcript_manifest_path=Path(args.transcript_manifest),
        output_dir=Path(args.output_dir),
        call_metadata_path=call_meta,
        activity_metadata_path=activity_meta,
        model=args.model,
        limit=args.limit,
        skip_existing=args.skip_existing,
    ))


__all__ = ["build_parser", "main"]
