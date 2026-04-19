"""Logging bootstrap. Kept in infrastructure because it wires into ``sys.stderr``."""
from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logger to write structured lines to stderr."""
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format=_FORMAT,
        datefmt=_DATE_FORMAT,
    )
