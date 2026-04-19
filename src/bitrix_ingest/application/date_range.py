"""Helpers for inclusive date-range filtering."""
from __future__ import annotations

import re
from datetime import datetime, timezone

_PLAIN_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_range_bound(value: str | None, *, end_of_day: bool) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if _PLAIN_DATE.fullmatch(text):
        suffix = "T23:59:59" if end_of_day else "T00:00:00"
        return f"{text}{suffix}"
    return text


def build_closed_filter(
    field: str | None,
    *,
    date_from: str | None,
    date_to: str | None,
) -> dict[str, str]:
    if not field:
        return {}
    filter_: dict[str, str] = {}
    normalized_from = normalize_range_bound(date_from, end_of_day=False)
    normalized_to = normalize_range_bound(date_to, end_of_day=True)
    if normalized_from:
        filter_[f">={field}"] = normalized_from
    if normalized_to:
        filter_[f"<={field}"] = normalized_to
    return filter_


def within_datetime_range(
    raw_value: str | None,
    *,
    date_from: str | None,
    date_to: str | None,
) -> bool:
    parsed = _parse_datetime(raw_value)
    if parsed is None:
        return True

    default_tzinfo = parsed.tzinfo
    lower = _parse_datetime(
        normalize_range_bound(date_from, end_of_day=False),
        default_tzinfo=default_tzinfo,
    )
    upper = _parse_datetime(
        normalize_range_bound(date_to, end_of_day=True),
        default_tzinfo=default_tzinfo,
    )
    if lower is not None and parsed < lower:
        return False
    if upper is not None and parsed > upper:
        return False
    return True


def _parse_datetime(
    value: str | None,
    *,
    default_tzinfo=None,
) -> datetime | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=default_tzinfo or timezone.utc)
    return parsed
