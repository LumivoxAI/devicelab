"""Internal timestamp calibration and bounded capture callback delivery."""

from __future__ import annotations

import time
from typing import Any
from threading import Lock, Event
from dataclasses import dataclass
from collections.abc import Callable

import numpy as np
from lumivox_core.logger import Logger

from lumivox_devicelab.errors import PipelineError
from lumivox_devicelab.capture import CapturedChunk, CaptureContext, CaptureHandler
from lumivox_devicelab.formats import AudioFormat

from .runtime import GStreamerElementError
from .elements.app import CapturePacket, CapturePacketError, CapturePacketFlags
from .pipeline_runtime import _WorkerContext

_PULL_TIMEOUT_MS = 100
_DEFAULT_MAX_BATCH_PACKETS = 8
_NANOSECONDS_PER_SECOND = 1_000_000_000

_PacketPuller = Callable[[int], CapturePacket | None]
_HealthObserver = Callable[[CapturePacketError], None]


@dataclass(slots=True)
class _GenerationChange:
    context: CaptureContext
    cause: PipelineError
    completed: Event


def calibrate_capture_context(
    pipeline: Any,
    *,
    audio_format: AudioFormat,
    generation: int,
    wall_time_ns: Callable[[], int] = time.time_ns,
) -> CaptureContext:
    """Calibrate one generation against the pipeline clock with bracketed wall reads."""
    before_ns = wall_time_ns()
    clock = pipeline.get_clock()
    if clock is None:
        raise GStreamerElementError("GStreamer pipeline has no clock")
    clock_time_ns = int(clock.get_time())
    base_time_ns = int(pipeline.get_base_time())
    after_ns = wall_time_ns()
    running_time_ns = clock_time_ns - base_time_ns
    if running_time_ns < 0:
        raise GStreamerElementError("GStreamer pipeline clock precedes its base time")
    return CaptureContext(
        audio_format=audio_format,
        generation=generation,
        wall_time_anchor_ns=(before_ns + after_ns) // 2,
        running_time_anchor_ns=running_time_ns,
    )


class _CaptureDelivery:
    """Run every handler callback serially from one bounded delivery worker."""

    def __init__(
        self,
        *,
        logger: Logger,
        handler: CaptureHandler,
        pull_packet: _PacketPuller,
        health_observer: _HealthObserver,
        max_batch_packets: int = _DEFAULT_MAX_BATCH_PACKETS,
    ) -> None:
        if not isinstance(handler, CaptureHandler):
            raise TypeError("handler must be a CaptureHandler")
        if isinstance(max_batch_packets, bool) or not isinstance(max_batch_packets, int):
            raise TypeError("max_batch_packets must be an integer")
        if max_batch_packets <= 0:
            raise ValueError("max_batch_packets must be positive")
        self._logger = logger.bind(module="devicelab")
        self._handler = handler
        self._pull_packet = pull_packet
        self._health_observer = health_observer
        self._max_batch_packets = max_batch_packets

        self._lock = Lock()
        self._activation = Event()
        self._started = Event()
        self._eos = Event()
        self._initial_context: CaptureContext | None = None
        self._current_context: CaptureContext | None = None
        self._pending_generation: _GenerationChange | None = None
        self._force_discontinuity = False

    def activate(self, worker: _WorkerContext, context: CaptureContext) -> None:
        """Readiness hook that waits until on_start has completed."""
        with self._lock:
            if self._initial_context is not None:
                raise RuntimeError("capture delivery has already been activated")
            self._initial_context = context
            self._activation.set()
        while not self._started.wait(0.01):
            if worker.cancelled:
                return

    def notify_eos(self) -> None:
        """Tell delivery to drain all packets already accepted by AppSink."""
        self._eos.set()

    def force_discontinuity(self) -> None:
        """Force the next delivered packet to begin a discontinuous chunk."""
        with self._lock:
            self._force_discontinuity = True

    def restart(self, context: CaptureContext, cause: PipelineError) -> Event:
        """Schedule on_restart before any packet from the new generation."""
        completed = Event()
        with self._lock:
            current = self._current_context
            if current is None:
                raise RuntimeError("capture delivery has not started")
            if self._pending_generation is not None:
                raise RuntimeError("a capture generation change is already pending")
            if context.generation != current.generation + 1:
                raise ValueError("capture generation must increment by one")
            self._pending_generation = _GenerationChange(context, cause, completed)
        return completed

    def run(self, worker: _WorkerContext) -> None:
        """Worker target registered with the pipeline runtime."""
        while not self._activation.wait(0.01):
            if worker.cancelled:
                return
        with self._lock:
            context = self._initial_context
        assert context is not None

        try:
            self._handler.on_start(context)
        except Exception as error:
            worker.fail("capture handler on_start failed", error)
            return
        with self._lock:
            self._current_context = context
        self._started.set()

        while not worker.cancelled:
            if not self._apply_generation_change(worker):
                break
            packets = self._pull_batch(worker)
            if packets is None:
                break
            if not packets:
                if self._eos.is_set():
                    worker.stop()
                    break
                continue
            if not self._deliver_packets(worker, packets):
                break

        worker.wait_graph_closed()
        with self._lock:
            final_context = self._current_context
        assert final_context is not None
        try:
            self._handler.on_stop(final_context, worker.failure)
        except Exception as error:
            worker.fail("capture handler on_stop failed", error)

    def _apply_generation_change(self, worker: _WorkerContext) -> bool:
        with self._lock:
            change = self._pending_generation
            self._pending_generation = None
        if change is None:
            return True
        try:
            self._handler.on_restart(change.context, change.cause)
        except Exception as error:
            worker.fail("capture handler on_restart failed", error)
            change.completed.set()
            return False
        with self._lock:
            self._current_context = change.context
            self._force_discontinuity = True
        change.completed.set()
        return True

    def _pull_batch(self, worker: _WorkerContext) -> list[tuple[CapturePacket, bool]] | None:
        packets: list[tuple[CapturePacket, bool]] = []
        timeout_ms = _PULL_TIMEOUT_MS
        for _ in range(self._max_batch_packets):
            if worker.cancelled:
                return None
            try:
                packet = self._pull_packet(timeout_ms)
            except CapturePacketError as error:
                if not self._discard_packet(worker, error):
                    return None
                timeout_ms = 0
                continue
            if packet is None:
                break
            with self._lock:
                forced = self._force_discontinuity
                self._force_discontinuity = False
            packets.append((packet, forced))
            timeout_ms = 0
        return packets

    def _discard_packet(self, worker: _WorkerContext, error: CapturePacketError) -> bool:
        self.force_discontinuity()
        self._logger.warning("capture_packet_discarded", category=error.kind.value, error=str(error))
        try:
            self._health_observer(error)
        except Exception as observer_error:
            worker.fail("capture health observer failed", observer_error)
            return False
        return True

    def _deliver_packets(self, worker: _WorkerContext, packets: list[tuple[CapturePacket, bool]]) -> bool:
        group: list[CapturePacket] = []
        group_discontinuous = False
        for packet, forced in packets:
            try:
                self._validate_packet(packet)
            except Exception as error:
                worker.fail("invalid capture packet reached delivery", error)
                return False
            packet_discontinuous = forced or bool(packet.flags & (CapturePacketFlags.DISCONT | CapturePacketFlags.GAP))
            if group and (packet_discontinuous or not self._is_contiguous(group[-1], packet)):
                if not self._deliver_group(worker, group, group_discontinuous):
                    return False
                group = []
                group_discontinuous = True
            if not group:
                group_discontinuous = group_discontinuous or packet_discontinuous
            group.append(packet)
        return not group or self._deliver_group(worker, group, group_discontinuous)

    def _deliver_group(
        self,
        worker: _WorkerContext,
        packets: list[CapturePacket],
        discontinuous: bool,
    ) -> bool:
        with self._lock:
            context = self._current_context
        assert context is not None
        try:
            first = packets[0]
            samples = (
                packets[0].samples if len(packets) == 1 else np.concatenate([packet.samples for packet in packets])
            )
            captured_at_ns = context.wall_time_anchor_ns + first.running_time_ns - context.running_time_anchor_ns
            chunk = CapturedChunk(
                samples=samples,
                generation=context.generation,
                captured_at_ns=captured_at_ns,
                running_time_ns=first.running_time_ns,
                discontinuity=discontinuous,
            )
            self._handler.on_chunk(chunk)
        except Exception as error:
            worker.fail("capture handler on_chunk failed", error)
            return False
        return True

    def _validate_packet(self, packet: CapturePacket) -> None:
        with self._lock:
            context = self._current_context
        assert context is not None
        channels = context.audio_format.channels
        if channels == 1:
            valid_shape = packet.samples.ndim == 1
        else:
            valid_shape = packet.samples.ndim == 2 and packet.samples.shape[1] == channels
        if not valid_shape or packet.samples.shape[0] == 0:
            raise RuntimeError("capture packet shape does not match its generation context")

    def _is_contiguous(self, previous: CapturePacket, current: CapturePacket) -> bool:
        with self._lock:
            context = self._current_context
        assert context is not None
        expected_ns = previous.running_time_ns + previous.duration_ns
        frame_period_ns = (
            _NANOSECONDS_PER_SECOND + context.audio_format.sample_rate - 1
        ) // context.audio_format.sample_rate
        return abs(current.running_time_ns - expected_ns) <= frame_period_ns
