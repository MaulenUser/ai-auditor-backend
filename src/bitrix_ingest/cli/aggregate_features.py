"""CLI entry point for aggregating feature JSON files into dept-level statistics."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from ..application.analytics import AggregateFeatureRequest, AggregateFeatureService
from ..infrastructure.logging_config import setup_logging
from ..infrastructure.persistence import FileSystemJsonWriter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Aggregate feature JSON files into department-level statistics"
    )
    parser.add_argument(
        "--features-dir",
        default="export/call-features/features",
        help="Directory with feature JSON files (default: export/call-features/features)",
    )
    parser.add_argument(
        "--output-dir",
        default="export/call-analytics",
        help="Output directory (default: export/call-analytics)",
    )
    parser.add_argument("--limit", type=int, default=0, help="Max files to process (0 = all)")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    setup_logging()
    args = build_parser().parse_args(argv)
    AggregateFeatureService(sink=FileSystemJsonWriter()).execute(
        AggregateFeatureRequest(
            features_dir=Path(args.features_dir),
            output_dir=Path(args.output_dir),
            limit=args.limit,
        )
    )


__all__ = ["build_parser", "main"]
