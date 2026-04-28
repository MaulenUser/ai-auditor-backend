"""Run the sales-quality analyzer from the project root.

Reads the OpenAI key from .env key 'open-ai' unless --openai-api-key is passed.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ENV_FILE = Path(".env")


def _load_env_key(key: str) -> str | None:
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip()
    return None


def _path_or_none(value: str | None) -> Path | None:
    if not value or value.strip().lower() in {"none", "null", "-"}:
        return None
    return Path(value)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze sales-quality problems across calls and WhatsApp"
    )
    parser.add_argument(
        "--call-transcript-manifest",
        default="export/recordings/transcripts/transcripts_manifest.json",
    )
    parser.add_argument(
        "--call-metadata",
        default="export/call-records-scan/recording-candidates.json",
    )
    parser.add_argument(
        "--activity-metadata",
        default="export/call-records-scan/activities.source.json",
    )
    parser.add_argument(
        "--whatsapp-conversation-dir",
        default="export/whatsapp-timeline/conversations_filtered",
    )
    parser.add_argument("--users-path", default="")
    parser.add_argument("--output-dir", default="export/sales-quality")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--slow-response-threshold-sec", type=int, default=900)
    parser.add_argument("--max-chars-per-item", type=int, default=24000)
    parser.add_argument("--openai-api-key", metavar="KEY")
    args = parser.parse_args()

    api_key = args.openai_api_key or os.environ.get("OPENAI_API_KEY") or _load_env_key("open-ai")
    if not api_key:
        print(
            "ERROR: OpenAI API key not found. Add 'open-ai=sk-...' to .env, "
            "set OPENAI_API_KEY, or use --openai-api-key.",
            file=sys.stderr,
        )
        sys.exit(1)

    sys.path.insert(0, "src")
    from bitrix_ingest.application.sales_quality import (
        AnalyzeSalesQualityRequest,
        AnalyzeSalesQualityService,
    )
    from bitrix_ingest.infrastructure.openai import OpenAiResponsesClient
    from bitrix_ingest.infrastructure.persistence import FileSystemJsonWriter

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


if __name__ == "__main__":
    main()
