from __future__ import annotations

import time
from typing import cast
from threading import Lock, Event, Thread, current_thread
from collections import deque
from unittest.mock import Mock
from collections.abc import Callable

import numpy as np
import pytest

from lumivox_devicelab.errors import PipelineError
from lumivox_devicelab.capture import CapturedChunk, CaptureContext, CaptureHandler
from lumivox_devicelab.formats import AudioFormat
from lumivox_devicelab._gstreamer.elements.app import (
    CapturePacket,
    CapturePacketError,
    CapturePacketFlags,
    CapturePacketErrorKind,
)
from lumivox_devicelab._gstreamer.capture_delivery import _CaptureDelivery, calibrate_capture_context
from lumivox_devicelab._gstreamer.pipeline_runtime import _WorkerContext


class FakeWorker:
    def __init__(self, events: list[str]) -> None:
        self._cancelled = Event()
        self._graph_closed = Event()
        self._failure: PipelineError | None = None
        self.events = events

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def failure(self) -> PipelineError | None:
        return self._failure

    def stop(self) -> None:
        self.events.append("null")
        self._cancelled.set()
        self._graph_closed.set()

    def wait_cancelled(self, timeout: float | None = None) -> bool:
        return self._cancelled.wait(timeout)

    def begin_callback(self) -> bool:
        return not self.cancelled

    def fail(self, message: str, cause: BaseException) -> PipelineError:
        if self._failure is None:
            self._failure = PipelineError(message, cause=cause)
        else:
            self._failure._add_secondary_error(cause)
        self.stop()
        return self._failure

    def wait_graph_closed(self, timeout: float | None = None) -> bool:
        return self._graph_closed.wait(timeout)

    def use_graph(self, operation: Callable[[object], object]) -> object:
        return operation(object())


class PacketSource:
    def __init__(self, *items: CapturePacket | CapturePacketError) -> None:
        self._lock = Lock()
        self._items = deque(items)
        self.pulls = 0

    def add(self, *items: CapturePacket | CapturePacketError) -> None:
        with self._lock:
            self._items.extend(items)

    def __call__(self, timeout_ms: int) -> CapturePacket | None:
        del timeout_ms
        with self._lock:
            self.pulls += 1
            if not self._items:
                return None
            item = self._items.popleft()
        if isinstance(item, CapturePacketError):
            raise item
        return item


class RecordingHandler(CaptureHandler):
    def __init__(
        self,
        *,
        on_start: Callable[[], None] | None = None,
        on_chunk: Callable[[], None] | None = None,
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        self.events: list[str] = []
        self.chunks: list[CapturedChunk] = []
        self.callback_threads: list[int | None] = []
        self.stop_cause: PipelineError | None = None
        self._start_hook = on_start
        self._chunk_hook = on_chunk
        self._stop_hook = on_stop

    def on_start(self, context: CaptureContext) -> None:
        self.events.append(f"start:{context.generation}")
        self.callback_threads.append(current_thread().ident)
        if self._start_hook is not None:
            self._start_hook()

    def on_chunk(self, chunk: CapturedChunk) -> None:
        self.events.append(f"chunk:{chunk.generation}")
        self.chunks.append(chunk)
        self.callback_threads.append(current_thread().ident)
        if self._chunk_hook is not None:
            self._chunk_hook()

    def on_restart(self, context: CaptureContext, cause: PipelineError) -> None:
        del cause
        self.events.append(f"restart:{context.generation}")
        self.callback_threads.append(current_thread().ident)

    def on_stop(self, context: CaptureContext, cause: PipelineError | None) -> None:
        self.events.append(f"stop:{context.generation}")
        self.callback_threads.append(current_thread().ident)
        self.stop_cause = cause
        if self._stop_hook is not None:
            self._stop_hook()


def _context(generation: int = 0, *, channels: int = 1) -> CaptureContext:
    return CaptureContext(AudioFormat(1_000, channels), generation, 10_000_000, 0)


def _packet(
    start_ns: int,
    values: list[int] | list[list[int]],
    *,
    duration_ns: int | None = None,
    flags: CapturePacketFlags = CapturePacketFlags.NONE,
) -> CapturePacket:
    samples = np.array(values, dtype="<i2")
    frames = samples.shape[0]
    return CapturePacket(samples, start_ns, duration_ns if duration_ns is not None else frames * 1_000_000, flags)


def _delivery(
    handler: CaptureHandler,
    source: PacketSource,
    worker: FakeWorker,
    *,
    health: Mock | None = None,
    max_batch_packets: int = 8,
    context: CaptureContext | None = None,
) -> tuple[_CaptureDelivery, Thread]:
    logger = Mock()
    logger.bind.return_value = logger
    delivery = _CaptureDelivery(
        logger=logger,
        handler=handler,
        pull_packet=source,
        health_observer=health or Mock(),
        max_batch_packets=max_batch_packets,
    )
    thread = Thread(target=delivery.run, args=(cast(_WorkerContext, worker),), daemon=False)
    thread.start()
    delivery.activate(cast(_WorkerContext, worker), context or _context())
    return delivery, thread


def test_calibration_uses_bracketed_wall_midpoint_and_pipeline_running_time() -> None:
    wall_reads = iter((1_000, 1_200))
    pipeline = Mock()
    pipeline.get_clock.return_value.get_time.return_value = 900
    pipeline.get_base_time.return_value = 400

    context = calibrate_capture_context(
        pipeline,
        audio_format=AudioFormat(16_000, 1),
        generation=2,
        wall_time_ns=lambda: next(wall_reads),
    )

    assert context.wall_time_anchor_ns == 1_100
    assert context.running_time_anchor_ns == 500
    assert context.generation == 2


def test_delivery_aggregates_with_one_frame_tolerance_and_splits_larger_gap() -> None:
    source = PacketSource(
        _packet(0, [1, 2]),
        _packet(3_000_000, [3]),
        _packet(5_000_001, [4]),
    )
    worker = FakeWorker([])
    handler = RecordingHandler()
    delivery, thread = _delivery(handler, source, worker)
    delivery.notify_eos()
    thread.join(1)

    assert not thread.is_alive()
    assert [chunk.samples.tolist() for chunk in handler.chunks] == [[1, 2, 3], [4]]
    assert [chunk.running_time_ns for chunk in handler.chunks] == [0, 5_000_001]
    assert [chunk.captured_at_ns for chunk in handler.chunks] == [10_000_000, 15_000_001]
    assert [chunk.discontinuity for chunk in handler.chunks] == [False, True]
    assert handler.events == ["start:0", "chunk:0", "chunk:0", "stop:0"]


@pytest.mark.parametrize(
    "kind",
    [
        CapturePacketErrorKind.MAPPING,
        CapturePacketErrorKind.MALFORMED,
        CapturePacketErrorKind.MISSING_TIMESTAMP,
    ],
)
def test_discarded_packets_report_health_and_force_next_boundary(kind: CapturePacketErrorKind) -> None:
    discarded = CapturePacketError(kind, "bad packet")
    source = PacketSource(_packet(0, [1]), discarded, _packet(1_000_000, [2]))
    worker = FakeWorker([])
    health = Mock()
    handler = RecordingHandler()
    delivery, thread = _delivery(handler, source, worker, health=health)
    delivery.notify_eos()
    thread.join(1)

    health.assert_called_once_with(discarded)
    assert [chunk.samples.tolist() for chunk in handler.chunks] == [[1], [2]]
    assert [chunk.discontinuity for chunk in handler.chunks] == [False, True]


def test_restart_updates_context_before_chunks_and_marks_first_boundary() -> None:
    source = PacketSource()
    worker = FakeWorker([])
    handler = RecordingHandler()
    delivery, thread = _delivery(handler, source, worker)
    restarted = delivery.restart(_context(1), PipelineError("device restarted"))
    assert restarted.wait(1)
    source.add(_packet(0, [7]))
    deadline = time.monotonic() + 1
    while not handler.chunks and time.monotonic() < deadline:
        time.sleep(0.001)
    assert handler.chunks
    delivery.notify_eos()
    thread.join(1)

    assert handler.events == ["start:0", "restart:1", "chunk:1", "stop:1"]
    assert handler.chunks[0].generation == 1
    assert handler.chunks[0].discontinuity


def test_pause_prevents_pulls_and_cancellation_suppresses_pending_restart_callback() -> None:
    source = PacketSource()
    worker = FakeWorker([])
    handler = RecordingHandler()
    delivery, thread = _delivery(handler, source, worker)

    assert delivery.pause(cast(_WorkerContext, worker))
    pulls_when_paused = source.pulls
    completed = delivery.restart(_context(1), PipelineError("device failed"))
    worker.stop()
    delivery.resume()
    thread.join(1)

    assert not thread.is_alive()
    assert source.pulls == pulls_when_paused
    assert completed.is_set()
    assert handler.events == ["start:0", "stop:1"]


def test_eos_delivers_tail_and_on_stop_runs_after_graph_null_on_same_worker() -> None:
    events: list[str] = []
    source = PacketSource(_packet(0, [[1, 2]], duration_ns=1_000_000))
    worker = FakeWorker(events)
    handler = RecordingHandler(on_stop=lambda: events.append("on_stop"))
    delivery, thread = _delivery(handler, source, worker, context=_context(channels=2))
    delivery.notify_eos()
    thread.join(1)

    assert handler.chunks[0].samples.shape == (1, 2)
    assert events == ["null", "on_stop"]
    callback_threads = [ident for ident in handler.callback_threads if ident is not None]
    assert callback_threads and len(set(callback_threads)) == 1


def test_handler_failure_is_retained_and_on_stop_failure_is_secondary() -> None:
    primary = RuntimeError("chunk failed")
    secondary = RuntimeError("stop failed")

    def fail_chunk() -> None:
        raise primary

    def fail_stop() -> None:
        raise secondary

    source = PacketSource(_packet(0, [1]))
    worker = FakeWorker([])
    handler = RecordingHandler(on_chunk=fail_chunk, on_stop=fail_stop)
    delivery, thread = _delivery(handler, source, worker)
    thread.join(1)

    assert worker.failure is handler.stop_cause
    assert worker.failure is not None
    assert worker.failure.__cause__ is primary
    assert worker.failure.secondary_errors == (secondary,)


def test_on_stop_failure_is_primary_after_normal_eos() -> None:
    original = RuntimeError("stop failed")

    def fail_stop() -> None:
        raise original

    worker = FakeWorker([])
    handler = RecordingHandler(on_stop=fail_stop)
    delivery, thread = _delivery(handler, PacketSource(), worker)
    delivery.notify_eos()
    thread.join(1)

    assert worker.failure is not None
    assert worker.failure.__cause__ is original


def test_stop_requested_inside_callback_does_not_wait_for_delivery_thread() -> None:
    worker = FakeWorker([])
    handler = RecordingHandler(on_chunk=worker.stop)
    delivery, thread = _delivery(handler, PacketSource(_packet(0, [1])), worker)
    thread.join(1)

    assert not thread.is_alive()
    assert handler.events == ["start:0", "chunk:0", "stop:0"]
    assert worker.failure is None


def test_delivery_never_pulls_more_than_bounded_batch_while_callback_is_slow() -> None:
    callback_entered = Event()
    release_callback = Event()

    def block_callback() -> None:
        callback_entered.set()
        assert release_callback.wait(1)

    source = PacketSource(*(_packet(index * 2_000_000, [index]) for index in range(20)))
    worker = FakeWorker([])
    handler = RecordingHandler(on_chunk=block_callback)
    delivery, thread = _delivery(handler, source, worker, max_batch_packets=4)
    assert callback_entered.wait(1)
    pulls_while_blocked = source.pulls
    release_callback.set()
    delivery.notify_eos()
    thread.join(1)

    assert pulls_while_blocked == 4
    assert not thread.is_alive()
