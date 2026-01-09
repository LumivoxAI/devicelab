"""Deterministic capture-health windows and restart budgeting."""

from __future__ import annotations

import time
from threading import Lock
from collections import deque
from collections.abc import Callable

from .elements.app import CapturePacketError, CapturePacketErrorKind

_WINDOW_SECONDS = 10.0
_STABLE_RESET_SECONDS = 60.0
_RESTART_DELAYS = (0.25, 1.0, 4.0)


class _RollingWindow:
    def __init__(self, threshold: int, monotonic: Callable[[], float]) -> None:
        self._threshold = threshold
        self._monotonic = monotonic
        self._events: deque[float] = deque()

    def add(self) -> bool:
        now = self._monotonic()
        while self._events and now - self._events[0] >= _WINDOW_SECONDS:
            self._events.popleft()
        self._events.append(now)
        return len(self._events) >= self._threshold

    def clear(self) -> None:
        self._events.clear()


class _CaptureHealth:
    """Classify packet faults and report when microphone recovery is required."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._mapping = _RollingWindow(3, monotonic)
        self._severe = _RollingWindow(2, monotonic)
        self._lock = Lock()

    def observe(self, error: CapturePacketError) -> bool:
        with self._lock:
            if error.kind is CapturePacketErrorKind.CAPS:
                return True
            if error.kind is CapturePacketErrorKind.MAPPING:
                return self._mapping.add()
            if error.kind in (CapturePacketErrorKind.MALFORMED, CapturePacketErrorKind.MISSING_TIMESTAMP):
                return self._severe.add()
            raise RuntimeError(f"unsupported capture packet error category: {error.kind!r}")

    def clear(self) -> None:
        with self._lock:
            self._mapping.clear()
            self._severe.clear()


class _RestartBudget:
    """Track globally consumed attempts until capture has been stable for 60 seconds."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._consumed = 0
        self._running_since: float | None = monotonic()

    def mark_running(self) -> None:
        self._running_since = self._monotonic()

    def begin_recovery(self) -> None:
        now = self._monotonic()
        if self._running_since is not None and now - self._running_since >= _STABLE_RESET_SECONDS:
            self._consumed = 0
        self._running_since = None

    def next_delay(self) -> float | None:
        if self._consumed == len(_RESTART_DELAYS):
            return None
        delay = _RESTART_DELAYS[self._consumed]
        self._consumed += 1
        return delay
