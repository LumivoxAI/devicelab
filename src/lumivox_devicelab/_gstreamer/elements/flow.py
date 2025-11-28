"""GStreamer elements that control buffering, branching, and timing."""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from .base import BaseElement
from ..runtime import GStreamerElementError, get_gst


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
        self._requested_pads: list[Any] = []

    def link(self, next_element: BaseElement) -> BaseElement:
        template = self.impl.get_pad_template("src_%u")
        if template is None:
            raise GStreamerElementError(f"Failed to get request-pad template from {self}")
        source_pad = self.impl.request_pad(template, None, None)
        if source_pad is None:
            raise GStreamerElementError(f"Failed to request source pad from {self}")
        sink_pad = next_element.impl.get_static_pad("sink")
        if sink_pad is None:
            self.impl.release_request_pad(source_pad)
            raise GStreamerElementError(f"Failed to get sink pad from {next_element}")
        if source_pad.link(sink_pad) != get_gst().PadLinkReturn.OK:
            self.impl.release_request_pad(source_pad)
            raise GStreamerElementError(f"Failed to link {self} -> {next_element}")
        self._requested_pads.append(source_pad)
        return next_element

    def release_request_pads(self) -> None:
        for source_pad in self._requested_pads:
            self.impl.release_request_pad(source_pad)
        self._requested_pads.clear()


class ClockSync(BaseElement):
    """Synchronize a file source with the pipeline clock for real-time replay."""

    def __init__(self, name: str | None = None) -> None:
        super().__init__("clocksync", name)
        self.impl.set_property("sync", True)
        self.impl.set_property("sync-to-first", True)
