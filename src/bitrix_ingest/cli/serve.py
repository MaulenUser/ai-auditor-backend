"""CLI entry point for the REST API server."""
from __future__ import annotations

import argparse
from typing import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the Bitrix Ingest REST API server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes (dev)")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "uvicorn not found. Install the API extras:\n"
            "  pip install -e \".[api]\""
        ) from exc

    args = build_parser().parse_args(argv)
    uvicorn.run(
        "bitrix_ingest.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


__all__ = ["build_parser", "main"]
