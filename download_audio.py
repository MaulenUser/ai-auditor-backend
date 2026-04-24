"""Download audio attachments from filtered deal JSON files.

For each deal_N.json in INPUT_DIR that contains audio attachments,
creates a deal_N_audio/ folder under OUTPUT_BASE and downloads the files.

The original URLs are Bitrix internal ajax.php links that require a browser session.
This script resolves them via the REST API (disk.file.get) to get a proper download URL.

Reads 'wehhook-whatsapp-message' from .env in the project root automatically.

Usage:
    python download_audio.py
    python download_audio.py --webhook-base-url "https://portal.bitrix24.kz/rest/1/TOKEN/"
"""
import argparse
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

ENV_FILE = Path(".env")

INPUT_DIR = Path("export/whatsapp-timeline/conversations_filtered")
OUTPUT_BASE = Path("export/whatsapp-timeline/audio_downloaded")


def _load_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    result = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def _extract_file_id(url: str) -> str | None:
    """Extract fileId from a Bitrix ajax.php disk download URL."""
    qs = parse_qs(urlparse(url).query)
    ids = qs.get("fileId") or qs.get("id")
    return ids[0] if ids else None


def _rest_download_url(webhooks: list[str], file_id: str) -> str | None:
    """Try each webhook token until disk.file.get succeeds, return DOWNLOAD_URL."""
    for base in webhooks:
        api_url = f"{base.rstrip('/')}/disk.file.get.json"
        try:
            resp = requests.get(api_url, params={"id": file_id}, timeout=30)
            if resp.status_code == 401:
                continue  # try next token
            resp.raise_for_status()
            data = resp.json()
            result = data.get("result") or {}
            url = result.get("DOWNLOAD_URL")
            if url:
                return url
        except Exception as exc:  # noqa: BLE001
            print(f"  ERR disk.file.get(id={file_id}) via {base}: {exc}", file=sys.stderr)
    print(f"  ERR disk.file.get(id={file_id}): all tokens returned 401", file=sys.stderr)
    return None


def _filename_from_attachment(attachment: dict, index: int) -> str:
    label = attachment.get("label", "").strip()
    if label:
        return label
    url = attachment.get("url", "")
    qs = parse_qs(urlparse(url).query)
    if "fileName" in qs:
        return qs["fileName"][0]
    return f"audio_{index}.mp3"


def download_audio(deal_json: Path, output_dir: Path, webhook_base_url: list[str] | None) -> int:
    data = json.loads(deal_json.read_text(encoding="utf-8"))
    downloaded = 0

    for msg_idx, msg in enumerate(data.get("messages", [])):
        for att_idx, att in enumerate(msg.get("attachments", [])):
            if att.get("type") != "audio":
                continue
            original_url = att.get("url", "")
            if not original_url:
                continue

            filename = _filename_from_attachment(att, att_idx)
            dest = output_dir / filename
            if dest.exists():
                stem = dest.stem
                suffix = dest.suffix or ".mp3"
                dest = output_dir / f"{stem}_{msg_idx}_{att_idx}{suffix}"

            # resolve proper download URL via REST API
            download_url = original_url
            if webhook_base_url:
                file_id = _extract_file_id(original_url)
                if file_id:
                    resolved = _rest_download_url(webhook_base_url, file_id)
                    if resolved:
                        download_url = resolved
                    else:
                        print(f"  WARN {filename}: could not resolve via REST, trying original URL")

            try:
                resp = requests.get(download_url, timeout=120, stream=True)
                resp.raise_for_status()
                # sanity check: reject HTML responses
                content_type = resp.headers.get("content-type", "")
                if "text/html" in content_type:
                    print(
                        f"  ERR {filename}: server returned HTML (auth failed?)",
                        file=sys.stderr,
                    )
                    continue
                with open(dest, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=8192):
                        fh.write(chunk)
                print(f"  OK  {dest.name}  ({dest.stat().st_size:,} bytes)")
                downloaded += 1
            except requests.HTTPError as exc:
                print(f"  ERR {filename}: HTTP {exc.response.status_code}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"  ERR {filename}: {exc}", file=sys.stderr)

    return downloaded


def main() -> None:
    parser = argparse.ArgumentParser(description="Download audio attachments from deal JSONs")
    parser.add_argument(
        "--webhook-base-url",
        metavar="URL",
        help="Bitrix24 webhook URL used to resolve proper download URLs via REST API",
    )
    args = parser.parse_args()

    env = _load_env()
    if args.webhook_base_url:
        webhooks = [args.webhook_base_url]
        print("Using webhook from --webhook-base-url")
    else:
        # collect all webhook values from .env, try all of them for disk access
        webhooks = [v for k, v in env.items() if k.startswith("wehhook") and v]
        if webhooks:
            print(f"Using {len(webhooks)} webhook(s) from .env")
        else:
            print("No webhook URL — attempting direct download (may fail without auth)")

    files = sorted(INPUT_DIR.glob("deal_*.json"))
    if not files:
        print(f"No deal_*.json files found in {INPUT_DIR}", file=sys.stderr)
        sys.exit(1)

    total_files = total_downloaded = 0
    for deal_json in files:
        data = json.loads(deal_json.read_text(encoding="utf-8"))
        audio_atts = [
            att
            for msg in data.get("messages", [])
            for att in msg.get("attachments", [])
            if att.get("type") == "audio" and att.get("url")
        ]
        if not audio_atts:
            continue

        deal_name = deal_json.stem
        output_dir = OUTPUT_BASE / f"{deal_name}_audio"
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"{deal_json.name}: {len(audio_atts)} audio file(s) -> {output_dir}")
        count = download_audio(deal_json, output_dir, webhooks or None)
        total_downloaded += count
        total_files += 1

    print(f"\nDone. {total_downloaded} files downloaded from {total_files} deals.")


if __name__ == "__main__":
    main()
