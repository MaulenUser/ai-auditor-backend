"""Cursor-based Bitrix pagination extracted into its own iterable.

Making this a standalone class keeps the BitrixClient focused on a single
request and lets us reason about, and test, pagination in isolation.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from .client import BitrixClient

logger = logging.getLogger(__name__)

_DEFAULT_PAGE_SIZE = 50
_DEFAULT_ORDER: dict[str, Any] = {"ID": "ASC"}


@dataclass
class ListPaginator:
    """Iterate Bitrix ``*.list`` pages via the ``start``/``next`` cursor."""

    client: "BitrixClient"
    method: str
    select: list[str]
    filter: dict[str, Any] | None = None  # noqa: A003 - matches PS field
    order: dict[str, Any] | None = None
    context: str = ""
    page_delay: float = 0.0
    page_size: int = _DEFAULT_PAGE_SIZE
    limit: int | None = None
    _total_loaded: int = field(default=0, init=False, repr=False)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        self._total_loaded = 0
        if self.limit is not None and self.limit <= 0:
            return

        start = 0
        effective_order = self.order or _DEFAULT_ORDER
        effective_filter = self.filter or {}

        while True:
            page_num = (start // self.page_size) + 1
            label = self._build_label(page_num, start)
            response = self.client.call(
                self.method,
                body={
                    "select": self.select,
                    "filter": effective_filter,
                    "order": effective_order,
                    "start": start,
                },
                label=label,
            )

            result = response.get("result")
            if result is None:
                logger.info("%s: 0 rows, stopping pagination.", label)
                return

            rows: list[dict[str, Any]] = result if isinstance(result, list) else [result]
            if self.limit is not None:
                remaining = self.limit - self._total_loaded
                if remaining <= 0:
                    return
                rows = rows[:remaining]

            for row in rows:
                yield row
            self._total_loaded += len(rows)
            logger.info(
                "%s: loaded %d rows, total %d",
                label,
                len(rows),
                self._total_loaded,
            )

            if self.limit is not None and self._total_loaded >= self.limit:
                summary = f"{self.method} {self.context}".strip()
                logger.info(
                    "%s pagination stopped at limit %d. Total rows: %d",
                    summary,
                    self.limit,
                    self._total_loaded,
                )
                return

            next_start = response.get("next")
            if next_start is None:
                summary = f"{self.method} {self.context}".strip()
                logger.info(
                    "%s pagination completed on page %d. Total rows: %d",
                    summary,
                    page_num,
                    self._total_loaded,
                )
                return

            if self.page_delay > 0:
                time.sleep(self.page_delay)
            start = int(next_start)

    def _build_label(self, page_num: int, start: int) -> str:
        parts = [self.method]
        if self.context:
            parts.append(self.context)
        parts.append(f"page {page_num} (start={start})")
        return " ".join(parts)
