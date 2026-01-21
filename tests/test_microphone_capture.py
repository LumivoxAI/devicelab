from __future__ import annotations

import time
from typing import Any, TypeVar, cast
from pathlib import Path
from threading import Event
from unittest.mock import Mock
from collections.abc import Callable

import numpy as np
import pytest

from lumivox_devicelab.state import PipelineState
from lumivox_devicelab.errors import PipelineError, DeviceNotFoundError
from lumivox_devicelab.capture import CapturedChunk, CaptureContext, CaptureHandler
from lumivox_devicelab.formats import AudioFormat, ChannelSelection
from lumivox_devicelab.microphone import MicrophoneCapturePipeline
from lumivox_devicelab._gstreamer.graph import _PipelineGraph
from lumivox_devicelab._gstreamer.runtime import get_gst
from lumivox_devicelab._gstreamer.recording import _RecordingError
from lumivox_devicelab._gstreamer.elements.app import AppSinkPolicy, CapturePacketError, CapturePacketErrorKind
from lumivox_devicelab._gstreamer.elements.base import BaseElement
from lumivox_devicelab._gstreamer.elements.flow import QueueOverflowPolicy
from lumivox_devicelab._gstreamer.capture_recovery import _RestartBudget
from lumivox_devicelab._gstreamer.pipeline_runtime import _WorkerContext


class RecordingHandler(CaptureHandler):
    def __init__(self) -> None:
        self.started = Event()
        self.chunk_received = Event()
        self.stopped = Event()
        self.chunks: list[CapturedChunk] = []
        self.stop_cause: PipelineError | None = None

    def on_start(self, context: CaptureContext) -> None:
        del context
        self.started.set()

    def on_chunk(self, chunk: CapturedChunk) -> None:
        self.chunks.append(chunk)
        self.chunk_received.set()

    def on_stop(self, context: CaptureContext, cause: PipelineError | None) -> None:
        del context
        self.stop_cause = cause
        self.stopped.set()


def _logger() -> Mock:
    logger = Mock()
    logger.bind.return_value = logger
    return logger


def test_stop_is_graceful_by_default_and_immediate_is_explicit() -> None:
    pipeline = MicrophoneCapturePipeline(
        logger=_logger(),
        handler=RecordingHandler(),
        audio_format=AudioFormat(16_000, 1),
        device_id="stable-microphone",
    )
    runtime = Mock()
    pipeline._runtime = cast(Any, runtime)

    pipeline.stop()
    runtime.stop_gracefully.assert_called_once_with(pipeline._send_eos, timeout=10.0)
    runtime.stop.assert_not_called()

    pipeline.stop(immediate=True, timeout=2.0)
    runtime.stop.assert_called_once_with(timeout=2.0)

    with pytest.raises(TypeError, match="immediate must be a bool"):
        pipeline.stop(immediate=cast(Any, 1))


_T = TypeVar("_T")


class FakeRecoveryWorker:
    def __init__(self, *outcomes: CaptureContext | BaseException) -> None:
        self.cancel_event = Event()
        self.outcomes = list(outcomes)
        self.delays: list[float] = []
        self.failure: PipelineError | None = None

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def wait_cancelled(self, timeout: float | None = None) -> bool:
        if timeout is not None and timeout > 0:
            self.delays.append(timeout)
        return self.cancelled

    def restart_graph(
        self,
        readiness: Callable[[_WorkerContext], _T],
        commit: Callable[[_T], None] | None = None,
    ) -> _T:
        del readiness
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        result = cast(_T, outcome)
        if commit is not None:
            commit(result)
        return result

    def begin_callback(self) -> bool:
        return not self.cancelled

    def raise_if_restart_aborted(self) -> None:
        return

    def fail(self, message: str, cause: BaseException) -> PipelineError:
        self.failure = PipelineError(message, cause=cause)
        self.cancel_event.set()
        return self.failure


class FakeRecoveryDelivery:
    def __init__(self, worker: FakeRecoveryWorker, *, fail_restart: BaseException | None = None) -> None:
        self.worker = worker
        self.fail_restart = fail_restart
        self.pauses = 0
        self.resumes = 0
        self.discontinuities = 0
        self.restarts: list[tuple[CaptureContext, PipelineError]] = []

    def force_discontinuity(self) -> None:
        self.discontinuities += 1

    def pause(self, worker: _WorkerContext) -> bool:
        del worker
        self.pauses += 1
        return True

    def resume(self) -> None:
        self.resumes += 1

    def restart(self, context: CaptureContext, cause: PipelineError) -> Event:
        self.restarts.append((context, cause))
        completed = Event()
        if self.fail_restart is not None:
            self.worker.fail("capture handler on_restart failed", self.fail_restart)
        completed.set()
        return completed


def test_constructor_is_pure(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = Mock(side_effect=AssertionError("constructor accessed devices"))
    monkeypatch.setattr("lumivox_devicelab.microphone.resolve_pipewire_target", resolver)

    pipeline = MicrophoneCapturePipeline(
        logger=_logger(),
        handler=RecordingHandler(),
        audio_format=AudioFormat(16_000, 1),
        device_id="stable-node-name",
    )

    assert pipeline.state is PipelineState.CREATED
    assert pipeline.failure is None
    assert resolver.call_count == 0


def test_recording_configuration_is_validated_before_start(tmp_path: Path) -> None:
    def create(**kwargs: Any) -> MicrophoneCapturePipeline:
        return MicrophoneCapturePipeline(
            logger=_logger(),
            handler=RecordingHandler(),
            audio_format=AudioFormat(16_000, 1),
            device_id="stable-node-name",
            **kwargs,
        )

    with pytest.raises(ValueError, match="requires record_to"):
        create(overwrite=True)
    with pytest.raises(TypeError, match="overwrite must be a bool"):
        create(overwrite=cast(Any, 1))
    with pytest.raises(ValueError, match=".wav or .flac"):
        create(record_to=tmp_path / "capture.mp3")

    existing = tmp_path / "capture.wav"
    existing.write_bytes(b"do not truncate")
    with pytest.raises(ValueError, match="already exists"):
        create(record_to=existing)
    assert existing.read_bytes() == b"do not truncate"


def test_missing_device_failure_retains_original_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    original = DeviceNotFoundError("missing")
    monkeypatch.setattr(
        "lumivox_devicelab.microphone.resolve_pipewire_target",
        Mock(side_effect=original),
    )
    pipeline = MicrophoneCapturePipeline(
        logger=_logger(),
        handler=RecordingHandler(),
        audio_format=AudioFormat(16_000, 1),
        device_id="missing",
    )

    with pytest.raises(PipelineError) as raised:
        pipeline.start()

    assert raised.value is pipeline.failure
    assert raised.value.__cause__ is original
    assert pipeline.state is PipelineState.STOPPED


def test_explicit_mapping_builds_required_bounded_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class FakeElement:
        def __init__(self, kind: str, args: tuple[object, ...], kwargs: dict[str, object]) -> None:
            self.kind = kind
            created.append((kind, args, kwargs))

        def observe_caps(self, callback: object) -> None:
            del callback

    def factory(kind: str):  # type: ignore[no-untyped-def]
        return lambda *args, **kwargs: FakeElement(kind, args, kwargs)

    for name in (
        "PipeWireSrc",
        "SourceChannelCapsFilter",
        "AudioConvert",
        "AudioResample",
        "CapsFilter",
        "AudioQueue",
        "AppSink",
    ):
        monkeypatch.setattr(f"lumivox_devicelab.microphone.{name}", factory(name))

    graph = Mock(spec=_PipelineGraph)
    pipeline = MicrophoneCapturePipeline(
        logger=_logger(),
        handler=RecordingHandler(),
        audio_format=AudioFormat(16_000, 1),
        device_id="mapped",
        channel_selection=ChannelSelection(source_channels=2, mapping=(1,)),
    )
    pipeline._target_object = "42"
    pipeline._build_graph(graph)

    assert [item[0] for item in created] == [
        "PipeWireSrc",
        "AudioConvert",
        "AudioResample",
        "CapsFilter",
        "AudioQueue",
        "AppSink",
        "SourceChannelCapsFilter",
    ]
    source_call = created[0]
    assert source_call[2]["target_object"] == "42"
    source_caps_call = created[-1]
    assert source_caps_call[1] == (2,)
    queue_call = next(item for item in created if item[0] == "AudioQueue")
    assert queue_call[2]["max_time_ms"] == 200
    assert queue_call[2]["overflow_policy"] is QueueOverflowPolicy.DROP_OLD
    sink_call = next(item for item in created if item[0] == "AppSink")
    assert sink_call[2]["policy"] is AppSinkPolicy.LIVE_DROP
    linked = graph.link.call_args.args
    assert [element.kind for element in linked] == [
        "PipeWireSrc",
        "SourceChannelCapsFilter",
        "AudioConvert",
        "AudioResample",
        "CapsFilter",
        "AudioQueue",
        "AppSink",
    ]


def test_recording_graph_gives_each_tee_branch_a_bounded_queue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    created: list[tuple[str, dict[str, object]]] = []

    class FakeElement:
        def __init__(self, kind: str, kwargs: dict[str, object]) -> None:
            self.kind = kind
            created.append((kind, kwargs))

        def observe_caps(self, callback: object) -> None:
            del callback

    def factory(kind: str):  # type: ignore[no-untyped-def]
        return lambda *args, **kwargs: FakeElement(kind, kwargs)

    for name in ("PipeWireSrc", "AudioConvert", "AudioResample", "CapsFilter", "AudioQueue", "AppSink", "Tee"):
        monkeypatch.setattr(f"lumivox_devicelab.microphone.{name}", factory(name))
    recording = Mock()
    build_recording = Mock(return_value=recording)
    monkeypatch.setattr("lumivox_devicelab.microphone._RecordingBranch.build", build_recording)

    graph = Mock(spec=_PipelineGraph)
    pipeline = MicrophoneCapturePipeline(
        logger=_logger(),
        handler=RecordingHandler(),
        audio_format=AudioFormat(16_000, 1),
        device_id="recorded",
        record_to=tmp_path / "capture.wav",
    )
    pipeline._target_object = "42"
    pipeline._build_graph(graph)

    capture_queue = next(item for item in created if item[0] == "AudioQueue")
    assert capture_queue[1]["overflow_policy"] is QueueOverflowPolicy.DROP_OLD
    assert graph.branch.call_count == 1
    build_recording.assert_called_once()
    assert build_recording.call_args.kwargs["path"] == tmp_path / "capture.wav"
    assert cast(Any, pipeline)._recordings[graph] is recording

    next_graph = Mock(spec=_PipelineGraph)
    cast(Any, pipeline)._building_generation = 1
    pipeline._build_graph(next_graph)
    assert build_recording.call_args.kwargs["path"] == tmp_path / "capture.1.wav"


@pytest.mark.gstreamer(factories=("audiotestsrc", "audioconvert", "audioresample", "capsfilter", "queue", "appsink"))
def test_controlled_microphone_normalizes_and_stops_once(monkeypatch: pytest.MonkeyPatch) -> None:
    class TestSource(BaseElement):
        def __init__(self, **kwargs: object) -> None:
            del kwargs
            super().__init__("audiotestsrc", "controlled-source")
            self.impl.set_property("is-live", True)
            self.impl.set_property("samplesperbuffer", 160)

    monkeypatch.setattr("lumivox_devicelab.microphone.resolve_pipewire_target", lambda *args: "1")
    monkeypatch.setattr("lumivox_devicelab.microphone.PipeWireSrc", TestSource)
    handler = RecordingHandler()
    pipeline = MicrophoneCapturePipeline(
        logger=_logger(),
        handler=handler,
        audio_format=AudioFormat(16_000, 1),
        device_id="controlled",
    )

    pipeline.start()
    assert handler.started.is_set()
    assert handler.chunk_received.wait(2)
    pipeline.stop()

    assert pipeline.state is PipelineState.STOPPED
    assert pipeline.failure is None
    assert handler.stopped.is_set()
    assert handler.stop_cause is None
    assert handler.chunks
    assert all(chunk.samples.dtype == np.dtype("<i2") for chunk in handler.chunks)
    assert all(chunk.samples.ndim == 1 and chunk.samples.size for chunk in handler.chunks)


@pytest.mark.parametrize(
    ("suffix", "factories"),
    ((".wav", ("filesrc", "wavparse", "appsink")), (".flac", ("filesrc", "flacparse", "flacdec", "appsink"))),
)
@pytest.mark.gstreamer(
    factories=(
        "audiotestsrc",
        "audioconvert",
        "audioresample",
        "capsfilter",
        "queue",
        "tee",
        "appsink",
        "filesink",
        "wavenc",
        "flacenc",
        "filesrc",
        "wavparse",
        "flacparse",
        "flacdec",
    )
)
def test_controlled_microphone_recording_is_parseable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    suffix: str,
    factories: tuple[str, ...],
) -> None:
    class TestSource(BaseElement):
        def __init__(self, **kwargs: object) -> None:
            del kwargs
            super().__init__("audiotestsrc", "controlled-recording-source")
            self.impl.set_property("is-live", True)
            self.impl.set_property("samplesperbuffer", 160)

    monkeypatch.setattr("lumivox_devicelab.microphone.resolve_pipewire_target", lambda *args: "1")
    monkeypatch.setattr("lumivox_devicelab.microphone.PipeWireSrc", TestSource)
    path = tmp_path / f"capture{suffix}"
    handler = RecordingHandler()
    pipeline = MicrophoneCapturePipeline(
        logger=_logger(),
        handler=handler,
        audio_format=AudioFormat(16_000, 1),
        device_id="controlled",
        record_to=path,
    )

    pipeline.start()
    assert handler.chunk_received.wait(2)
    time.sleep(0.05)
    pipeline.stop()

    assert path.stat().st_size > 0
    gst = get_gst()
    reader = gst.Pipeline.new("recording-reader")
    elements = [gst.ElementFactory.make(factory) for factory in factories]
    assert reader is not None and all(element is not None for element in elements)
    source, *remaining = elements
    source.set_property("location", str(path))
    sink = remaining[-1]
    sink.set_property("sync", False)
    for element in elements:
        reader.add(element)
    for first, second in zip(elements, elements[1:]):
        assert first.link(second)
    try:
        assert reader.set_state(gst.State.PLAYING) != gst.StateChangeReturn.FAILURE
        sample = sink.emit("try-pull-sample", 2 * gst.SECOND)
        assert sample is not None
        structure = sample.get_caps().get_structure(0)
        assert structure.get_value("rate") == 16_000
        assert structure.get_value("channels") == 1
    finally:
        reader.set_state(gst.State.NULL)


def test_recovery_retries_failed_graphs_with_fixed_delays_then_retains_exhaustion() -> None:
    attempts = (RuntimeError("first"), RuntimeError("second"), RuntimeError("third"))
    worker = FakeRecoveryWorker(*attempts)
    delivery = FakeRecoveryDelivery(worker)
    pipeline = MicrophoneCapturePipeline(
        logger=_logger(),
        handler=RecordingHandler(),
        audio_format=AudioFormat(16_000, 1),
        device_id="controlled",
    )
    cast(Any, pipeline)._delivery = delivery
    cast(Any, pipeline)._restart_budget = _RestartBudget(lambda: 0.0)

    trigger = RuntimeError("device gone")
    pipeline._request_recovery("microphone source failed", trigger)
    pipeline._recover(cast(_WorkerContext, worker))

    assert worker.delays == [0.25, 1.0, 4.0]
    assert worker.failure is not None
    assert worker.failure.__cause__ is attempts[-1]
    assert delivery.pauses == 1
    assert delivery.resumes == 0
    assert delivery.discontinuities == 1


def test_successful_recovery_notifies_handler_generation_before_resuming() -> None:
    context = CaptureContext(AudioFormat(16_000, 1), 1, 100, 10)
    worker = FakeRecoveryWorker(context)
    delivery = FakeRecoveryDelivery(worker)
    logger = _logger()
    logger.info.side_effect = lambda *args, **kwargs: worker.cancel_event.set()
    pipeline = MicrophoneCapturePipeline(
        logger=logger,
        handler=RecordingHandler(),
        audio_format=AudioFormat(16_000, 1),
        device_id="controlled",
    )
    cast(Any, pipeline)._delivery = delivery
    cast(Any, pipeline)._restart_budget = _RestartBudget(lambda: 0.0)

    trigger = RuntimeError("device gone")
    pipeline._request_recovery("microphone source failed", trigger)
    pipeline._recover(cast(_WorkerContext, worker))

    assert pipeline._generation == 1
    assert delivery.resumes == 1
    assert len(delivery.restarts) == 1
    restarted_context, cause = delivery.restarts[0]
    assert restarted_context is context
    assert cause.__cause__ is trigger
    assert worker.failure is None


def test_on_restart_failure_is_terminal_and_is_not_retried() -> None:
    context = CaptureContext(AudioFormat(16_000, 1), 1, 100, 10)
    callback_error = RuntimeError("restart callback failed")
    worker = FakeRecoveryWorker(context, CaptureContext(AudioFormat(16_000, 1), 2, 200, 20))
    delivery = FakeRecoveryDelivery(worker, fail_restart=callback_error)
    pipeline = MicrophoneCapturePipeline(
        logger=_logger(),
        handler=RecordingHandler(),
        audio_format=AudioFormat(16_000, 1),
        device_id="controlled",
    )
    cast(Any, pipeline)._delivery = delivery
    cast(Any, pipeline)._restart_budget = _RestartBudget(lambda: 0.0)

    pipeline._request_recovery("microphone source failed", RuntimeError("device gone"))
    pipeline._recover(cast(_WorkerContext, worker))

    assert worker.failure is not None
    assert worker.failure.__cause__ is callback_error
    assert len(worker.outcomes) == 1
    assert pipeline._generation == 1


def test_failed_restart_commit_does_not_activate_or_publish_the_segment(tmp_path: Path) -> None:
    pipeline = MicrophoneCapturePipeline(
        logger=_logger(),
        handler=RecordingHandler(),
        audio_format=AudioFormat(16_000, 1),
        device_id="controlled",
        record_to=tmp_path / "capture.wav",
    )
    recording = Mock()
    graph = Mock(spec=_PipelineGraph)
    cast(Any, pipeline)._recordings[graph] = recording
    cast(Any, pipeline)._building_generation = 1
    callback_error = RuntimeError("restart callback failed")
    cast(Any, pipeline)._delivery.restart = Mock(side_effect=callback_error)

    with pytest.raises(RuntimeError, match="restart callback failed"):
        pipeline._commit_restart(
            CaptureContext(AudioFormat(16_000, 1), 1, 100, 10),
            PipelineError("source failed"),
        )

    recording.mark_active.assert_not_called()
    assert pipeline._generation == 0


def test_recording_segment_open_failure_is_terminal_and_is_not_retried() -> None:
    segment_error = _RecordingError("capture.1.wav already exists")
    worker = FakeRecoveryWorker(segment_error, CaptureContext(AudioFormat(16_000, 1), 1, 100, 10))
    delivery = FakeRecoveryDelivery(worker)
    pipeline = MicrophoneCapturePipeline(
        logger=_logger(),
        handler=RecordingHandler(),
        audio_format=AudioFormat(16_000, 1),
        device_id="controlled",
    )
    cast(Any, pipeline)._delivery = delivery
    cast(Any, pipeline)._restart_budget = _RestartBudget(lambda: 0.0)

    pipeline._request_recovery("microphone source failed", RuntimeError("device gone"))
    pipeline._recover(cast(_WorkerContext, worker))

    assert worker.failure is not None
    assert worker.failure.__cause__ is segment_error
    assert len(worker.outcomes) == 1


def test_only_current_pipewire_source_bus_errors_are_recoverable() -> None:
    pipeline = MicrophoneCapturePipeline(
        logger=_logger(),
        handler=RecordingHandler(),
        audio_format=AudioFormat(16_000, 1),
        device_id="controlled",
    )
    source = Mock()
    cast(Any, pipeline)._pipewire_source = source
    cast(Any, pipeline._runtime)._state = PipelineState.RUNNING

    cause = RuntimeError("device gone")
    assert not pipeline._handle_bus_error(object(), cause)
    assert pipeline._handle_bus_error(source.impl, cause)
    assert pipeline._recovery_cause is not None
    assert pipeline._recovery_cause.__cause__ is cause


def test_unexpected_eos_requests_recovery_but_initial_caps_mismatch_is_terminal() -> None:
    pipeline = MicrophoneCapturePipeline(
        logger=_logger(),
        handler=RecordingHandler(),
        audio_format=AudioFormat(16_000, 1),
        device_id="controlled",
    )
    cast(Any, pipeline._runtime)._state = PipelineState.RUNNING
    pipeline._unexpected_eos()
    assert pipeline._recovery_cause is not None

    startup = MicrophoneCapturePipeline(
        logger=_logger(),
        handler=RecordingHandler(),
        audio_format=AudioFormat(16_000, 1),
        device_id="controlled",
    )
    cast(Any, startup._runtime)._state = PipelineState.STARTING
    mismatch = CapturePacketError(CapturePacketErrorKind.CAPS, "bad caps")
    startup._caps_changed(mismatch)

    assert startup.failure is not None
    assert startup.failure.__cause__ is mismatch
    assert startup._recovery_cause is None
