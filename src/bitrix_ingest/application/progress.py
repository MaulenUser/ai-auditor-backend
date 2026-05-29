from __future__ import annotations

import logging
from typing import Any, Callable

ProgressCallback = Callable[[dict[str, Any]], None]

logger = logging.getLogger(__name__)


def emit_progress(callback: ProgressCallback | None, **payload: Any) -> None:
    if callback is None:
        return
    try:
        callback(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Progress callback failed: %s", exc)
