"""Transcribe downloaded call recordings and save transcript text files.

For each entry in export/recordings/manifest.json with STATUS=downloaded:
  - transcribes the MP3 via OpenAI Whisper
  - writes a .txt file to export/recordings/transcripts/
  - saves a transcripts_manifest.json with results

Reads 'open-ai' key from .env automatically.

Usage:
    python transcribe_recordings.py
    python transcribe_recordings.py --model gpt-4o-transcribe --language ru
    python transcribe_recordings.py --overwrite --openai-api-key sk-...
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ENV_FILE = Path(".env")
DEFAULT_MANIFEST = Path("export/recordings/manifest.json")
DEFAULT_OUTPUT = Path("export/recordings/transcripts")


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


def _load_manifest(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    # handle [[...]] wrapping
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], list):
        raw = raw[0]
    return raw if isinstance(raw, list) else [raw]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transcribe call recordings via OpenAI Whisper"
    )
    parser.add_argument(
        "--manifest-path",
        default=str(DEFAULT_MANIFEST),
        help=f"Path to recordings manifest.json (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT),
        help=f"Output directory for transcripts (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--model",
        default="gpt-4o-transcribe",
        help="OpenAI transcription model (default: gpt-4o-transcribe)",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Language hint, e.g. 'ru' or 'kk'. Omit to auto-detect (recommended for mixed speech)",
    )
    parser.add_argument(
        "--company-name",
        metavar="NAME",
        default=None,
        help=(
            "Название компании — добавляется в начало prompt чтобы Whisper "
            "правильно распознал его. Пример: 'Сапаплас'"
        ),
    )
    parser.add_argument(
        "--prompt",
        default=(
            "Бұл сату телефон қоңырауы. "
            "Тақырып: терезе, есік, рама, өлшем, монтаж, баға, жеткізу, тапсырыс."
        ),
        help="Prompt hint for the transcription model",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-transcribe even if transcript .txt already exists",
    )
    parser.add_argument(
        "--openai-api-key",
        metavar="KEY",
        help="Override OpenAI API key (fallback: 'open-ai' in .env)",
    )
    args = parser.parse_args()

    prompt = args.prompt
    if args.company_name:
        prompt = f"{args.company_name}. {prompt}"

    api_key = args.openai_api_key or _load_env_key("open-ai")
    if not api_key:
        print(
            "ERROR: OpenAI API key not found. Add 'open-ai=sk-...' to .env or use --openai-api-key",
            file=sys.stderr,
        )
        sys.exit(1)

    sys.path.insert(0, "src")
    from bitrix_ingest.infrastructure.openai.transcription_client import (
        OpenAiTranscriptionClient,
    )

    client = OpenAiTranscriptionClient(api_key=api_key)

    manifest_path = Path(args.manifest_path)
    if not manifest_path.exists():
        print(f"ERROR: manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    all_entries = _load_manifest(manifest_path)
    entries = [
        e
        for e in all_entries
        if isinstance(e, dict)
        and str(e.get("STATUS") or "") in ("downloaded", "skipped_existing")
        and str(e.get("FILE_PATH") or "").strip()
    ]
    if not entries:
        print(f"No downloaded entries found in {manifest_path}", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Found {len(entries)} recording(s) to process")
    print(f"Output dir: {output_dir}\n")

    results: list[dict] = []
    total_transcribed = total_skipped = total_errors = 0

    for entry in entries:
        crm_id = str(entry.get("CRM_ACTIVITY_ID") or "")
        record_id = str(entry.get("RECORD_FILE_ID") or "")
        audio_path = Path(str(entry.get("FILE_PATH") or ""))
        label = f"CRM_ACTIVITY_ID={crm_id} ({audio_path.name})"

        txt_path = output_dir / f"{audio_path.stem}.txt"

        if not audio_path.exists():
            print(f"  SKIP {label}: audio file not found", file=sys.stderr)
            results.append({
                **entry,
                "TRANSCRIPT_STATUS": "error",
                "TRANSCRIPT_ERROR": "audio_file_not_found",
            })
            total_errors += 1
            continue

        if not args.overwrite and txt_path.exists():
            print(f"  SKIP {label}: transcript already exists")
            results.append({
                **entry,
                "TRANSCRIPT_STATUS": "skipped_existing",
                "TRANSCRIPT_PATH": str(txt_path),
            })
            total_skipped += 1
            continue

        try:
            response = client.transcribe(
                file_path=audio_path,
                model=args.model,
                language=args.language,
                prompt=prompt,
            )
            text = (response.get("text") or "").strip()
            if text:
                txt_path.write_text(text, encoding="utf-8")
                total_transcribed += 1
                preview = text[:100] + ("…" if len(text) > 100 else "")
                print(f"  OK  {label}: {preview}")
                results.append({
                    **entry,
                    "TRANSCRIPT_STATUS": "transcribed",
                    "TRANSCRIPT_PATH": str(txt_path),
                    "TEXT_LENGTH": len(text),
                })
            else:
                print(f"  EMPTY {label}: empty transcription result")
                results.append({**entry, "TRANSCRIPT_STATUS": "empty"})
                total_skipped += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERR {label}: {exc}", file=sys.stderr)
            results.append({
                **entry,
                "TRANSCRIPT_STATUS": "error",
                "TRANSCRIPT_ERROR": str(exc),
            })
            total_errors += 1

    summary_path = output_dir / "transcripts_manifest.json"
    summary_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\nDone. Transcribed: {total_transcribed}, Skipped: {total_skipped}, Errors: {total_errors}")
    print(f"Transcripts saved to: {output_dir}")
    print(f"Summary manifest:     {summary_path}")


if __name__ == "__main__":
    main()
