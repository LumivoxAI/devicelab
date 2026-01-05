"""GStreamer application source and sink elements."""

from __future__ import annotations

from enum import Enum, IntFlag
from typing import TYPE_CHECKING, Any
from dataclasses import dataclass

import numpy as np

from .base import BaseElement
from ..audio import S16LE_DTYPE, RawAudioSpec, validate_pcm_array
from ..runtime import GStreamerElementError, get_gst

if TYPE_CHECKING:
    from gi.repository import Gst  # type: ignore[import-not-found]

_MAX_APP_SRC_BUFFER_TIME_MS = 20
_NANOSECONDS_PER_SECOND = 1_000_000_000
_LIVE_APP_SINK_BUFFERS = 1
_BATCH_APP_SINK_BUFFERS = 8


class AppSinkPolicy(Enum):
    """Bounded buffering behavior for one capture use case."""

    LIVE_DROP = "live_drop"
    BATCH_BLOCK = "batch_block"


class CapturePacketFlags(IntFlag):
    """Backend-neutral buffer flags relevant to capture delivery."""

    NONE = 0
    DISCONT = 1
    GAP = 2


class CapturePacketErrorKind(Enum):
    """Health category for a packet that cannot be delivered."""

    CAPS = "caps"
    MAPPING = "mapping"
    MALFORMED = "malformed"
    MISSING_TIMESTAMP = "missing_timestamp"


class CapturePacketError(GStreamerElementError):
    """A discarded AppSink packet with a stable health category."""

    def __init__(self, kind: CapturePacketErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class CapturePacket:
    """One copied, timestamped buffer pulled from AppSink."""

    samples: np.ndarray
    running_time_ns: int
    duration_ns: int
    flags: CapturePacketFlags


class AppSink(BaseElement):
    """Pull normalized audio buffers from GStreamer as copied NumPy arrays."""

    def __init__(
        self,
        spec: RawAudioSpec,
        name: str | None = None,
        *,
        policy: AppSinkPolicy = AppSinkPolicy.LIVE_DROP,
        max_buffers: int | None = None,
    ) -> None:
        if not isinstance(policy, AppSinkPolicy):
            raise TypeError("policy must be an AppSinkPolicy")
        if max_buffers is None:
            max_buffers = _LIVE_APP_SINK_BUFFERS if policy is AppSinkPolicy.LIVE_DROP else _BATCH_APP_SINK_BUFFERS
        if isinstance(max_buffers, bool) or not isinstance(max_buffers, int):
            raise TypeError("max_buffers must be an integer")
        if max_buffers <= 0:
            raise ValueError("max_buffers must be positive")
        super().__init__("appsink", name)
        self._spec = spec
        self._segment_key: tuple[object, ...] | None = None
        self.impl.set_property("emit-signals", False)
        self.impl.set_property("sync", False)
        self.impl.set_property("max-buffers", max_buffers)
        self.impl.set_property("max-bytes", 0)
        self.impl.set_property("max-time", 0)
        self.impl.set_property("drop", policy is AppSinkPolicy.LIVE_DROP)
        self.impl.set_property("enable-last-sample", False)
        self.impl.set_property("wait-on-eos", False)

    def try_pull(self, timeout_ms: int = 100) -> CapturePacket | None:
        if timeout_ms < 0:
            raise ValueError("timeout_ms must not be negative")
        sample = self.impl.emit("try-pull-sample", timeout_ms * 1_000_000)
        if sample is None:
            return None
        return self._to_packet(sample)

    def validate_negotiated_caps(self) -> bool:
        """Return false until caps exist and reject an incompatible fixed result."""
        pad = self.impl.get_static_pad("sink")
        caps = None if pad is None else pad.get_current_caps()
        if caps is None or caps.is_empty() or caps.is_any():
            return False
        self._validate_caps(caps)
        return True

    def _to_packet(self, sample: Any) -> CapturePacket:
        self._validate_sample_caps(sample)
        buffer = sample.get_buffer()
        if buffer is None:
            raise CapturePacketError(CapturePacketErrorKind.MALFORMED, "appsink sample has no buffer")
        mapped, map_info = buffer.map(get_gst().MapFlags.READ)
        if not mapped or map_info is None:
            raise CapturePacketError(CapturePacketErrorKind.MAPPING, "failed to map appsink buffer")
        try:
            if map_info.size == 0:
                raise CapturePacketError(CapturePacketErrorKind.MALFORMED, "appsink buffer is empty")
            if map_info.size % self._spec.frame_size != 0:
                raise CapturePacketError(
                    CapturePacketErrorKind.MALFORMED,
                    f"appsink buffer size {map_info.size} is not a multiple of frame size {self._spec.frame_size}",
                )
            data = np.frombuffer(memoryview(map_info.data), dtype=S16LE_DTYPE).copy()
            if self._spec.channels > 1:
                data = data.reshape(-1, self._spec.channels)

            pts = buffer.pts
            gst = get_gst()
            if pts == gst.CLOCK_TIME_NONE:
                raise CapturePacketError(CapturePacketErrorKind.MISSING_TIMESTAMP, "appsink buffer has no PTS")
            segment = sample.get_segment()
            if segment is None:
                raise CapturePacketError(CapturePacketErrorKind.MISSING_TIMESTAMP, "appsink sample has no segment")
            running_time = segment.to_running_time(gst.Format.TIME, pts)
            if running_time == gst.CLOCK_TIME_NONE:
                raise CapturePacketError(
                    CapturePacketErrorKind.MISSING_TIMESTAMP,
                    "appsink PTS cannot be converted to running time",
                )

            flags = CapturePacketFlags.NONE
            if buffer.has_flags(gst.BufferFlags.DISCONT):
                flags |= CapturePacketFlags.DISCONT
            if buffer.has_flags(gst.BufferFlags.GAP):
                flags |= CapturePacketFlags.GAP
            segment_key = self._get_segment_key(segment)
            if self._segment_key is not None and segment_key != self._segment_key:
                flags |= CapturePacketFlags.DISCONT
            self._segment_key = segment_key

            frames = map_info.size // self._spec.frame_size
            duration_ns = frames * _NANOSECONDS_PER_SECOND // self._spec.rate
            return CapturePacket(data, int(running_time), duration_ns, flags)
        finally:
            buffer.unmap(map_info)

    def _validate_sample_caps(self, sample: Any) -> None:
        caps = sample.get_caps()
        if caps is None or caps.is_empty() or caps.is_any():
            raise CapturePacketError(CapturePacketErrorKind.CAPS, "appsink sample has no fixed caps")
        self._validate_caps(caps)

    def _validate_caps(self, caps: Any) -> None:
        structure = caps.get_structure(0)
        if structure is None:
            raise CapturePacketError(CapturePacketErrorKind.CAPS, "appsink sample caps have no structure")
        success, rate = structure.get_int("rate")
        channels_success, channels = structure.get_int("channels")
        if (
            structure.get_name() != "audio/x-raw"
            or structure.get_string("format") != "S16LE"
            or structure.get_string("layout") != "interleaved"
            or not success
            or rate != self._spec.rate
            or not channels_success
            or channels != self._spec.channels
        ):
            raise CapturePacketError(
                CapturePacketErrorKind.CAPS,
                "appsink caps do not match the requested S16LE interleaved audio specification",
            )

    @staticmethod
    def _get_segment_key(segment: Any) -> tuple[object, ...]:
        return tuple(
            getattr(segment, field, None)
            for field in ("format", "flags", "rate", "applied_rate", "base", "offset", "start", "stop", "time")
        )


class AppSrc(BaseElement):
    """Push validated NumPy PCM buffers into GStreamer."""

    def __init__(
        self,
        spec: RawAudioSpec,
        max_queue_time_ms: int = 250,
        block_when_full: bool = True,
        name: str | None = None,
    ) -> None:
        if max_queue_time_ms <= 0:
            raise ValueError("max_queue_time_ms must be positive")
        super().__init__("appsrc", name)
        gst = get_gst()
        max_queue_bytes = spec.frame_size * spec.rate * max_queue_time_ms // 1_000
        self.impl.set_property(
            "caps",
            gst.Caps.from_string(
                f"audio/x-raw,format=S16LE,layout=interleaved,rate={spec.rate},channels={spec.channels}"
            ),
        )
        self.impl.set_property("format", gst.Format.TIME)
        self.impl.set_property("is-live", True)
        self.impl.set_property("do-timestamp", False)
        self.impl.set_property("emit-signals", False)
        self.impl.set_property("block", block_when_full)
        self.impl.set_property("max-time", 0)
        self.impl.set_property("max-buffers", 0)
        self.impl.set_property("max-bytes", max_queue_bytes)
        self._spec = spec
        self._frames_pushed = 0
        self._max_buffer_frames = max(1, spec.rate * _MAX_APP_SRC_BUFFER_TIME_MS // 1_000)

    def push(self, data: np.ndarray) -> Gst.FlowReturn:
        """Push PCM data and return GStreamer's result for the final buffer."""
        frames = validate_pcm_array(data, self._spec)
        gst = get_gst()
        flow_return: Gst.FlowReturn = gst.FlowReturn.OK
        for start_frame in range(0, frames, self._max_buffer_frames):
            chunk = data[start_frame : start_frame + self._max_buffer_frames]
            raw = chunk.tobytes(order="C")
            buffer = gst.Buffer.new_allocate(None, len(raw), None)
            buffer.fill(0, raw)
            start_ns = self._frames_pushed * gst.SECOND // self._spec.rate
            end_frame = self._frames_pushed + len(chunk)
            end_ns = end_frame * gst.SECOND // self._spec.rate
            buffer.pts = start_ns
            buffer.duration = end_ns - start_ns
            flow_return = self.impl.emit("push-buffer", buffer)
            if flow_return != gst.FlowReturn.OK:
                return flow_return
            self._frames_pushed = end_frame
        return flow_return

    def push_eos(self) -> Gst.FlowReturn:
        """Signal that no further buffers will be pushed to this source."""
        return self.impl.emit("end-of-stream")
