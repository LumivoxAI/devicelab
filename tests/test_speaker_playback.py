from __future__ import annotations

from typing import Any, cast
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock
from collections.abc import Callable

import numpy as np
import pytest

from lumivox_devicelab.state import PipelineState
from lumivox_devicelab.errors import PipelineError, PipelineStateError, PlaybackSubmissionError
from lumivox_devicelab.formats import AudioFormat
from lumivox_devicelab.speaker import SpeakerPlaybackPipeline
from lumivox_devicelab._gstreamer.graph import _PipelineGraph
from lumivox_devicelab._gstreamer.elements.app import PlaybackPushResult
from lumivox_devicelab._gstreamer.elements.flow import QueueOverflowPolicy


def _logger() -> Mock:
    logger = Mock()
    logger.bind.return_value = logger
    return logger


class FakeRuntime:
    def __init__(self) -> None:
        self.state = PipelineState.RUNNING
        self.failure: PipelineError | None = None
        self.cancelled = False
        self.pipeline = object()
        self.graceful_started = Event()
        self.completed = Event()

    def use_graph(self, operation: Callable[[object], Any]) -> Any:
        return operation(self.pipeline)

    def begin_graceful_stop(self) -> None:
        if self.failure is not None:
            raise self.failure
        if self.state is not PipelineState.RUNNING:
            raise PipelineStateError(f"cannot gracefully stop pipeline in state {self.state.value}")
        self.state = PipelineState.STOPPING
        self.graceful_started.set()

    def complete_graceful_stop(self, deadline: float, timeout_error: object) -> None:
        del deadline, timeout_error
        self.state = PipelineState.STOPPED
        self.completed.set()

    def report_failure(self, message: str, cause: BaseException) -> None:
        self.failure = PipelineError(message, cause=cause)
        self.cancelled = True
        self.state = PipelineState.STOPPED

    def stop(self, *, timeout: float = 5.0) -> None:
        del timeout
        self.cancelled = True
        self.state = PipelineState.STOPPED


def _pipeline() -> tuple[SpeakerPlaybackPipeline, FakeRuntime]:
    pipeline = SpeakerPlaybackPipeline(
        logger=_logger(),
        audio_format=AudioFormat(16_000, 1),
        device_id="stable-speaker",
    )
    runtime = FakeRuntime()
    pipeline._runtime = cast(Any, runtime)
    return pipeline, runtime


def _call_in_thread(operation: Callable[[], None]) -> tuple[Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            operation()
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run, daemon=False)
    thread.start()
    return thread, errors


def test_constructor_is_pure(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = Mock(side_effect=AssertionError("constructor accessed devices"))
    monkeypatch.setattr("lumivox_devicelab.speaker.resolve_pipewire_target", resolver)

    pipeline = SpeakerPlaybackPipeline(
        logger=_logger(),
        audio_format=AudioFormat(16_000, 1),
        device_id="stable-speaker",
    )

    assert pipeline.state is PipelineState.CREATED
    assert pipeline.failure is None
    assert resolver.call_count == 0


@pytest.mark.parametrize("state", [PipelineState.CREATED, PipelineState.STARTING])
def test_graceful_stop_before_running_uses_runtime_stop(state: PipelineState) -> None:
    pipeline = SpeakerPlaybackPipeline(
        logger=_logger(),
        audio_format=AudioFormat(16_000, 1),
        device_id="stable-speaker",
    )
    runtime = Mock(state=state, failure=None)
    pipeline._runtime = cast(Any, runtime)

    pipeline.stop()

    timeout = runtime.stop.call_args.kwargs["timeout"]
    assert 0 < timeout <= 10.0
    runtime.begin_graceful_stop.assert_not_called()


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ([], TypeError),
        (np.array([], dtype=np.int16), ValueError),
        (np.array([0], dtype=np.float32), ValueError),
        (np.zeros((1, 1), dtype=np.int16), ValueError),
    ],
)
def test_invalid_submission_is_an_argument_error_without_failing_pipeline(data: object, error: type[Exception]) -> None:
    pipeline, runtime = _pipeline()

    with pytest.raises(error):
        pipeline.submit(cast(Any, data))

    assert runtime.failure is None


def test_recording_configuration_is_validated_before_start(tmp_path: Path) -> None:
    def create(**kwargs: Any) -> SpeakerPlaybackPipeline:
        return SpeakerPlaybackPipeline(
            logger=_logger(),
            audio_format=AudioFormat(16_000, 1),
            device_id="stable-speaker",
            **kwargs,
        )

    with pytest.raises(ValueError, match="requires record_to"):
        create(overwrite=True)
    with pytest.raises(ValueError, match=".wav or .flac"):
        create(record_to=tmp_path / "played.mp3")

    existing = tmp_path / "played.wav"
    existing.write_bytes(b"keep")
    with pytest.raises(ValueError, match="already exists"):
        create(record_to=existing)
    assert existing.read_bytes() == b"keep"


@pytest.mark.parametrize("recording", [False, True])
def test_graph_uses_only_required_bounded_queues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    recording: bool,
) -> None:
    created: list[tuple[str, dict[str, object]]] = []

    class FakeElement:
        def __init__(self, kind: str, kwargs: dict[str, object]) -> None:
            self.kind = kind
            self.impl = object()
            created.append((kind, kwargs))

    def factory(kind: str) -> Callable[..., FakeElement]:
        return lambda *args, **kwargs: FakeElement(kind, kwargs)

    for name in ("AppSrc", "AudioConvert", "AudioResample", "CapsFilter", "PipeWireSink", "Tee", "AudioQueue"):
        monkeypatch.setattr(f"lumivox_devicelab.speaker.{name}", factory(name))
    recording_branch = Mock()
    recording_branch.build.return_value = Mock()
    monkeypatch.setattr("lumivox_devicelab.speaker._RecordingBranch", recording_branch)

    path = tmp_path / "played.wav" if recording else None
    pipeline = SpeakerPlaybackPipeline(
        logger=_logger(),
        audio_format=AudioFormat(16_000, 1),
        device_id="stable-speaker",
        record_to=path,
    )
    pipeline._target_object = "42"
    graph = Mock(spec=_PipelineGraph)
    pipeline._build_graph(graph)

    queues = [kwargs for kind, kwargs in created if kind == "AudioQueue"]
    if recording:
        assert len(queues) == 1
        assert queues[0]["max_time_ms"] == 250
        assert queues[0]["overflow_policy"] is QueueOverflowPolicy.BLOCK
        recording_branch.build.assert_called_once()
        assert graph.branch.call_count == 1
    else:
        assert queues == []
        recording_branch.build.assert_not_called()
        graph.branch.assert_not_called()


def test_concurrent_submissions_are_serialized_as_complete_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline, _ = _pipeline()
    first_entered = Event()
    release_first = Event()
    order: list[int] = []

    class SerialAppSrc:
        def push(self, data: np.ndarray, running_time: object) -> PlaybackPushResult:
            del running_time
            value = int(data[0])
            order.append(value)
            if value == 1:
                first_entered.set()
                assert release_first.wait(1)
            order.append(value)
            return PlaybackPushResult(0, len(data))

    pipeline._app_src = cast(Any, SerialAppSrc())
    monkeypatch_gst = Mock()
    monkeypatch_gst.FlowReturn.OK = 0
    monkeypatch.setattr("lumivox_devicelab.speaker.get_gst", lambda: monkeypatch_gst)
    first, first_errors = _call_in_thread(lambda: pipeline.submit(np.array([1], dtype=np.int16)))
    assert first_entered.wait(1)
    second, second_errors = _call_in_thread(lambda: pipeline.submit(np.array([2], dtype=np.int16)))
    release_first.set()
    first.join(1)
    second.join(1)

    assert first_errors == []
    assert second_errors == []
    assert order == [1, 1, 2, 2]


def test_immediate_stop_interrupts_partial_submit_without_retained_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline, runtime = _pipeline()
    entered = Event()
    release = Event()

    class InterruptedAppSrc:
        def push(self, data: np.ndarray, running_time: object) -> PlaybackPushResult:
            del data, running_time
            entered.set()
            assert release.wait(1)
            return PlaybackPushResult(1, 320)

    pipeline._app_src = cast(Any, InterruptedAppSrc())
    gst = Mock()
    gst.FlowReturn.OK = 0
    monkeypatch.setattr("lumivox_devicelab.speaker.get_gst", lambda: gst)
    submitter, errors = _call_in_thread(lambda: pipeline.submit(np.zeros(640, dtype=np.int16)))
    assert entered.wait(1)

    pipeline.stop(immediate=True)
    release.set()
    submitter.join(1)

    assert runtime.failure is None
    assert len(errors) == 1
    assert isinstance(errors[0], PlaybackSubmissionError)
    submission_error = errors[0]
    assert submission_error.accepted_frames == 320
    assert isinstance(submission_error.__cause__, PipelineStateError)


def test_bus_failure_is_submission_cause_and_remains_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline, runtime = _pipeline()
    failure = PipelineError("bus failed", cause=RuntimeError("device gone"))
    runtime.failure = failure
    runtime.state = PipelineState.STOPPED

    with pytest.raises(PipelineError) as raised:
        pipeline.submit(np.zeros(1, dtype=np.int16))

    assert raised.value is failure


def test_graceful_stop_waits_for_active_submit_and_rejects_later_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline, runtime = _pipeline()
    submit_entered = Event()
    release_submit = Event()

    class DrainAppSrc:
        def push(self, data: np.ndarray, running_time: object) -> PlaybackPushResult:
            del running_time
            submit_entered.set()
            assert release_submit.wait(1)
            return PlaybackPushResult(0, len(data))

        def push_eos(self) -> int:
            pipeline._eos.set()
            return 0

    pipeline._app_src = cast(Any, DrainAppSrc())
    gst = Mock()
    gst.FlowReturn.OK = 0
    monkeypatch.setattr("lumivox_devicelab.speaker.get_gst", lambda: gst)

    submitter, submit_errors = _call_in_thread(lambda: pipeline.submit(np.zeros(1, dtype=np.int16)))
    assert submit_entered.wait(1)
    stopper, stop_errors = _call_in_thread(pipeline.stop)
    assert not runtime.graceful_started.wait(0.05)
    release_submit.set()
    assert runtime.graceful_started.wait(1)
    submitter.join(1)
    stopper.join(1)

    assert submit_errors == []
    assert stop_errors == []
    assert runtime.completed.is_set()
    with pytest.raises(PipelineStateError):
        pipeline.submit(np.zeros(1, dtype=np.int16))
    pipeline.stop()


@pytest.mark.parametrize("value", [0, 1, None, "yes"])
def test_stop_requires_boolean_immediate(value: object) -> None:
    pipeline, _ = _pipeline()

    with pytest.raises(TypeError, match="immediate must be a bool"):
        pipeline.stop(immediate=cast(Any, value))
