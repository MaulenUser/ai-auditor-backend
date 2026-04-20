"""CLI entry point for transcribing call recordings via OpenAI Whisper."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from ..application.transcribe import TranscribeRecordingsRequest, TranscribeRecordingsService
from ..infrastructure.logging_config import setup_logging
from ..infrastructure.openai import OpenAiTranscriptionClient
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
        description="Transcribe call recordings using OpenAI Whisper "
        "(port of transcribe-recordings-openai.ps1)"
    )
    parser.add_argument(
        "--manifest-path",
        default="export/recordings/manifest.json",
        help="Path to recordings manifest.json (default: export/recordings/manifest.json)",
    )
    parser.add_argument(
        "--output-dir", default="export/transcripts",
        help="Output directory (default: export/transcripts)",
    )
    parser.add_argument("--api-key", help="OpenAI API key (fallback: OPENAI_API_KEY env var)")
    parser.add_argument("--model", default="gpt-4o-transcribe", help="Whisper model name")
    parser.add_argument("--language", help="Language code, e.g. ru")
    parser.add_argument("--prompt", help="Optional transcription prompt")
    parser.add_argument("--limit", type=int, default=0, help="Max entries (0 = all)")
    parser.add_argument("--skip-existing", action="store_true", help="Skip already-transcribed files")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    setup_logging()
    args = build_parser().parse_args(argv)
    api_key = _resolve_api_key(args.api_key)
    TranscribeRecordingsService(
        gateway=OpenAiTranscriptionClient(api_key),
        sink=FileSystemJsonWriter(),
    ).execute(TranscribeRecordingsRequest(
        manifest_path=Path(args.manifest_path),
        output_dir=Path(args.output_dir),
        model=args.model,
        language=args.language,
        prompt=args.prompt,
        limit=args.limit,
        skip_existing=args.skip_existing,
    ))


__all__ = ["build_parser", "main"]
