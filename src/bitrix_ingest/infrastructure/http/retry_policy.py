"""Retry policy and backoff strategies.

Split out from the HTTP client so the retry behaviour is:
  - unit-testable in isolation,
  - reusable across different transports,
  - swappable without touching the client (Open/Closed principle).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Protocol


DEFAULT_TRANSIENT_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
DEFAULT_TRANSIENT_KEYWORDS: tuple[str, ...] = (
    "timed out",
    "timeout",
    "temporarily unavailable",
    "connection reset",
    "connection aborted",
    "remote end closed connection without response",
    "remotedisconnected",
    "the remote server returned an error",
)


class BackoffStrategy(Protocol):
    """A delay-computing strategy, parameterised on the attempt number (1-based)."""

    def delay_for(self, attempt: int) -> float: ...


@dataclass(frozen=True)
class ExponentialBackoff:
    """Exponential backoff with additive jitter.

    attempt=1 → base_seconds · 2^0 = 1 s (+ up to 20 % jitter)
    attempt=2 → base_seconds · 2^1 = 2 s
    attempt=3 → base_seconds · 2^2 = 4 s
    ...
    """

    base_seconds: float = 1.0
    jitter_ratio: float = 0.2

    def delay_for(self, attempt: int) -> float:
        base = self.base_seconds * float(2 ** (attempt - 1))
        jitter = random.uniform(0, base * self.jitter_ratio)
        return base + jitter


class TransientErrorClassifier:
    """Knows which errors deserve a retry.

    Pulling this classification out of the client lets us extend it (new
    status codes, new connection-error keywords) without editing the client.
    """

    def __init__(
        self,
        status_codes: frozenset[int] = DEFAULT_TRANSIENT_STATUS_CODES,
        keywords: tuple[str, ...] = DEFAULT_TRANSIENT_KEYWORDS,
    ) -> None:
        self._status_codes = status_codes
        self._keywords = keywords

    def is_transient_status(self, status_code: int | None) -> bool:
        return status_code in self._status_codes

    def is_transient_message(self, message: str) -> bool:
        lowered = message.lower()
        return any(keyword in lowered for keyword in self._keywords)


@dataclass(frozen=True)
class RetryPolicy:
    """Bundles how many times to retry with how long to wait."""

    max_attempts: int = 4
    backoff: BackoffStrategy = ExponentialBackoff()
    classifier: TransientErrorClassifier = TransientErrorClassifier()

    def should_retry(self, attempt: int) -> bool:
        return attempt < self.max_attempts
