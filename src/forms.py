"""
forms.py — reading numbers that people (or the AI) typed.

`float("abc")`, `float("nan")`, `int(float("1e400"))` and an 20-digit price that
SQLite can't store all used to surface as "Internal Server Error". These
helpers either return a sane finite number or raise ValueError with a message
fit to show the user — callers already treat ValueError as "check your input".
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

# Large enough for any real price, quantity or rate; small enough for SQLite's 64-bit integers.
MAX_VALUE = 1_000_000_000_000


def number(raw: Any, *, default: Optional[float] = 0.0, minimum: Optional[float] = None,
           maximum: Optional[float] = MAX_VALUE, label: str = "Value") -> float:
    """A finite float from user input. Blank → `default` (or an error when default is None)."""
    text = "" if raw is None else str(raw).strip().replace(",", "")
    if text == "":
        if default is None:
            raise ValueError(f"{label} is required.")
        return default
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{label} must be a number.") from None
    if not math.isfinite(value):
        raise ValueError(f"{label} must be a number.")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} can't be less than {minimum:g}.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} is too large.")
    return value


def integer(raw: Any, **kwargs) -> int:
    return int(round(number(raw, **kwargs)))


def cents(raw: Any, *, label: str = "Amount", maximum_units: float = 100_000_000) -> int:
    """'1,234.50' → 123450 exactly (decimal, not float: '1.005' is 101, not 100)."""
    text = "" if raw is None else str(raw).strip().replace(",", "")
    if text == "":
        return 0
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{label} must be a number.") from None
    if not value.is_finite() or value < 0 or value > Decimal(str(maximum_units)):
        raise ValueError(f"{label} must be between 0 and {maximum_units:,.0f}.")
    return int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
