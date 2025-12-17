"""Shared validation helpers for public Devicelab contracts."""

from __future__ import annotations

import math
from numbers import Real

GST_INT_MAX = 2**31 - 1


def validate_gst_positive_int(value: object, *, name: str) -> int:
    """Validate a positive value representable by a GStreamer signed integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 < value <= GST_INT_MAX:
        raise ValueError(f"{name} must be between 1 and {GST_INT_MAX}")
    return value


def validate_non_negative_int(value: object, *, name: str) -> int:
    """Validate a non-negative Python integer, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def validate_timeout(timeout: object, *, allow_none: bool = False) -> float | None:
    """Return a positive finite timeout in seconds."""
    if timeout is None:
        if allow_none:
            return None
        raise TypeError("timeout must be a positive finite number")
    if isinstance(timeout, bool) or not isinstance(timeout, Real):
        raise TypeError("timeout must be a positive finite number")

    result = float(timeout)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("timeout must be a positive finite number")
    return result
