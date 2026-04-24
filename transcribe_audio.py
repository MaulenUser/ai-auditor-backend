"""Transcribe downloaded audio files and embed results back into conversations_filtered JSONs.

For each deal_N.json in CONVERSATIONS_DIR:
  - finds audio attachments
  - locates the matching downloaded file in AUDIO_BASE/deal_N_audio/
  - transcribes via OpenAI Whisper
  - writes the transcription into the message's `text` field
  - saves the updated JSON back in place

Reads `open-ai` from .env automatically.

Usage:
    python transcribe_audio.py
    python transcribe_audio.py --model whisper-1 --language ru
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ENV_FILE = Path(".env")
CONVERSATIONS_DIR = Path("export/whatsapp-timeline/conversations_filtered")
AUDIO_BASE = Path("export/whatsapp-timeline/audio_downloaded")


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


def _find_audio_file(audio_dir: Path, label: str) -> Path | None:
    """Find downloaded audio file by its label (filename)."""
    candidate = audio_dir / label
    if candidate.exists():
        return candidate
    # fallback: match by stem in case extension differs
    stem = Path(label).stem.lower()
    for f in audio_dir.iterdir():
        if f.stem.lower() == stem:
            return f
    return None


def transcribe_deal(
    deal_json: Path,
    transcription_client,
    model: str,
    language: str,
    prompt: str | None = None,
    overwrite: bool = False,
) -> tuple[int, int]:
    """Return (transcribed_count, skipped_count)."""
    data = json.loads(deal_json.read_text(encoding="utf-8"))
    deal_stem = deal_json.stem          # deal_37448
    audio_dir = AUDIO_BASE / f"{deal_stem}_audio"

    transcribed = skipped = 0
    changed = False

    for msg in data.get("messages", []):
        for att in msg.get("attachments", []):
            if att.get("type") != "audio":
                continue
            label = att.get("label", "")
            if not label:
                continue

            # skip if message already has text (previously transcribed), unless overwrite
            if not overwrite and msg.get("text", "").strip():
                skipped += 1
                continue

            if not audio_dir.exists():
                print(f"  SKIP {label}: audio dir not found ({audio_dir})", file=sys.stderr)
                skipped += 1
                continue

            audio_file = _find_audio_file(audio_dir, label)
            if not audio_file:
                print(f"  SKIP {label}: file not found in {audio_dir}", file=sys.stderr)
                skipped += 1
                continue

            try:
                result = transcription_client.transcribe(
                    file_path=audio_file,
                    model=model,
                    language=language,
                    prompt=prompt,
                )
                text = (result.get("text") or "").strip()
                if text:
                    msg["text"] = text
                    att["transcribed"] = True
                    changed = True
                    transcribed += 1
                    print(f"  OK  {label}: {text[:80]}{'…' if len(text) > 80 else ''}")
                else:
                    print(f"  EMPTY {label}: empty transcription result")
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  ERR {label}: {exc}", file=sys.stderr)
                skipped += 1

    if changed:
        deal_json.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return transcribed, skipped


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transcribe audio attachments and embed text into conversation JSONs"
    )
    parser.add_argument(
        "--model",
        default="gpt-4o-transcribe",
        help="OpenAI transcription model (default: gpt-4o-transcribe)",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Audio language hint, e.g. 'kk' or 'ru'. Omit to auto-detect (recommended for mixed speech)",
    )
    parser.add_argument(
        "--prompt",
        default=(
            "Бұл WhatsApp арқылы жүргізілген сату әңгімесі. "
            "Тақырып: терезе, есік, рама, өлшем, монтаж, баға, жеткізу, келісім, тапсырыс."
        ),
        help="Prompt hint for the transcription model",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-transcribe even if message already has text",
    )
    parser.add_argument("--openai-api-key", metavar="KEY", help="Override OpenAI API key")
    args = parser.parse_args()

    api_key = args.openai_api_key or _load_env_key("open-ai")
    if not api_key:
        print("ERROR: OpenAI API key not found. Add 'open-ai=sk-...' to .env", file=sys.stderr)
        sys.exit(1)

    # import here so the script fails fast on missing package
    import sys as _sys
    _sys.path.insert(0, "src")
    from bitrix_ingest.infrastructure.openai.transcription_client import OpenAiTranscriptionClient

    client = OpenAiTranscriptionClient(api_key=api_key)

    files = sorted(CONVERSATIONS_DIR.glob("deal_*.json"))
    if not files:
        print(f"No deal_*.json files found in {CONVERSATIONS_DIR}", file=sys.stderr)
        sys.exit(1)

    total_transcribed = total_skipped = 0
    for deal_json in files:
        data = json.loads(deal_json.read_text(encoding="utf-8"))
        audio_attachments = [
            att
            for msg in data.get("messages", [])
            for att in msg.get("attachments", [])
            if att.get("type") == "audio" and att.get("label")
        ]
        if not audio_attachments:
            continue

        print(f"{deal_json.name}: {len(audio_attachments)} audio attachment(s)")
        t, s = transcribe_deal(deal_json, client, args.model, args.language, args.prompt, args.overwrite)
        total_transcribed += t
        total_skipped += s

    print(f"\nDone. Transcribed: {total_transcribed}, Skipped/errors: {total_skipped}")
    print(f"Updated JSONs saved in: {CONVERSATIONS_DIR}")


if __name__ == "__main__":
    main()
