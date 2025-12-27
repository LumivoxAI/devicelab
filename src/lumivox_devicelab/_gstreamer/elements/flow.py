"""GStreamer elements that control buffering, branching, and timing."""

from __future__ import annotations

from enum import IntEnum

from .base import BaseElement


class QueueOverflowPolicy(IntEnum):
    BLOCK = 0  # block upstream when full, no drops
    DROP_NEW = 1  # drop new incoming buffers when full
    DROP_OLD = 2  # drop old buffered data when full (best for real-time audio)


class AudioQueue(BaseElement):
    def __init__(
        self,
        max_time_ms: int = 200,
        overflow_policy: QueueOverflowPolicy = QueueOverflowPolicy.DROP_OLD,
        name: str | None = None,
    ) -> None:
        if max_time_ms <= 0:
            raise ValueError("max_time_ms must be positive")
        super().__init__("queue", name)
        self.impl.set_property("max-size-time", max_time_ms * 1_000_000)
        self.impl.set_property("max-size-buffers", 0)
        self.impl.set_property("max-size-bytes", 0)
        self.impl.set_property("leaky", int(overflow_policy))


class Tee(BaseElement):
    def __init__(self, name: str | None = None) -> None:
        super().__init__("tee", name)


class ClockSync(BaseElement):
    """Synchronize a file source with the pipeline clock for real-time replay."""

    def __init__(self, name: str | None = None) -> None:
        super().__init__("clocksync", name)
        self.impl.set_property("sync", True)
        self.impl.set_property("sync-to-first", True)
