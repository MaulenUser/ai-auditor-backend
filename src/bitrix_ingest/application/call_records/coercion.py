"""Type coercion helpers that mirror PowerShell's ``Get-StringValue`` / ``Get-IntValue``.

Bitrix returns most fields as stringly-typed JSON. These helpers centralise
the rules for converting ``None`` / empty / non-numeric values to sane
defaults so the domain entities don't have to re-implement the coercion
logic per field.
"""
from __future__ import annotations

from typing import Any


def coerce_str(value: Any) -> str:
    """Return the string form of *value*, or ``""`` when ``value is None``."""
    return "" if value is None else str(value)


def coerce_int(value: Any) -> int:
    """Round *value* to the nearest int; return ``0`` on anything un-parseable.

    Matches the PS contract: empty strings, whitespace, ``None``, and
    non-numeric strings all collapse to zero rather than raising.
    """
    if value is None:
        return 0
    try:
        return int(round(float(str(value).strip())))
    except (ValueError, TypeError):
        return 0
