from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, cast
from threading import Lock, Event, Thread, Condition
from unittest.mock import Mock, call
from collections.abc import Callable

import pytest

from lumivox_devicelab.state import PipelineState
from lumivox_devicelab.errors import PipelineError, PipelineStateError, PipelineTimeoutError
from lumivox_devicelab._gstreamer import pipeline_runtime as runtime_module
from lumivox_devicelab._gstreamer.graph import _PipelineGraph
from lumivox_devicelab._gstreamer.runtime import GStreamerElementError
from lumivox_devicelab._gstreamer.pipeline_runtime import _WorkerContext, _PipelineRuntime


class FakeMessage:
    def __init__(self, message_type: int, *, source: object, error: str = "bus failure") -> None:
        self.type = message_type
        self.src = source
        self.error = error
        self.parsed_error = RuntimeError(error)

    def parse_warning(self) -> tuple[RuntimeError, str]:
        return RuntimeError(self.error), "warning debug"

    def parse_error(self) -> tuple[RuntimeError, str]:
        return self.parsed_error, "error debug"

    def parse_state_changed(self) -> tuple[int, int, int]:
        return 0, 2, 0


class FakeBus:
    def __init__(self) -> None:
        self._condition = Condition(Lock())
        self._messages: list[FakeMessage] = []

    def push(self, message: FakeMessage) -> None:
        with self._condition:
            self._messages.append(message)
            self._condition.notify_all()

    def timed_pop_filtered(self, timeout_ns: int, message_types: int) -> FakeMessage | None:
        del message_types
        deadline = time.monotonic() + timeout_ns / 1_000_000_000
        with self._condition:
            while not self._messages:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return self._messages.pop(0)


class FakePipeline:
    def __init__(self, gst: SimpleNamespace, *, announce_playing: bool = True, state_result: int = 0) -> None:
        self.gst = gst
        self.bus = FakeBus()
        self.announce_playing = announce_playing
        self.state_result = state_result
        self.playing_requested = Event()

    def set_state(self, state: int) -> int:
        self.playing_requested.set()
        if self.announce_playing and self.state_result != self.gst.StateChangeReturn.FAILURE:
            self.bus.push(FakeMessage(self.gst.MessageType.STATE_CHANGED, source=self))
        return self.state_result

    def get_bus(self) -> FakeBus:
        return self.bus


class FakeGraph:
    def __init__(self, pipeline: FakePipeline, *, release_errors: tuple[BaseException, ...] = ()) -> None:
        self.pipeline = pipeline
        self.release_errors = release_errors
        self.released = Event()
        self._cleanup_complete = False
        self._lock = Lock()

    def use(self, operation: Callable[[Any], Any]) -> Any:
        with self._lock:
            if self.released.is_set():
                raise GStreamerElementError("GStreamer graph has been released")
            pipeline = self.pipeline
        return operation(pipeline)

    @property
    def cleanup_complete(self) -> bool:
        with self._lock:
            return self._cleanup_complete

    def close(self) -> tuple[BaseException, ...]:
        self.released.set()
        return ()

    def wait_idle(self, timeout: float) -> bool:
        del timeout
        return True

    def release(self) -> tuple[BaseException, ...]:
        with self._lock:
            self.released.set()
            self._cleanup_complete = True
            return self.release_errors


def _gst() -> SimpleNamespace:
    return SimpleNamespace(
        State=SimpleNamespace(PLAYING=2),
        StateChangeReturn=SimpleNamespace(FAILURE=-1),
        MessageType=SimpleNamespace(ERROR=1, WARNING=2, EOS=4, STATE_CHANGED=8),
    )


def _runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    announce_playing: bool = True,
    state_result: int = 0,
    readiness: Callable[[_WorkerContext], None] | None = None,
    on_eos: Callable[[], None] | None = None,
    release_errors: tuple[BaseException, ...] = (),
    build_graph: Callable[[_PipelineGraph], None] | None = None,
) -> tuple[_PipelineRuntime, FakeGraph, Mock, SimpleNamespace]:
    gst = _gst()
    monkeypatch.setattr(runtime_module, "get_gst", lambda: gst)
    pipeline = FakePipeline(gst, announce_playing=announce_playing, state_result=state_result)
    graph = FakeGraph(pipeline, release_errors=release_errors)
    logger = Mock()
    logger.bind.return_value = logger
    runtime = _PipelineRuntime(
        logger=logger,
        graph_factory=lambda: cast(_PipelineGraph, graph),
        build_graph=build_graph,
        readiness=readiness,
        on_eos=on_eos,
    )
    return runtime, graph, logger, gst


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


def _assert_state(runtime: _PipelineRuntime, expected: PipelineState) -> None:
    assert runtime.state is expected


def test_start_waits_for_playing_and_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    readiness_entered = Event()
    allow_readiness = Event()

    def readiness(context: _WorkerContext) -> None:
        readiness_entered.set()
        assert allow_readiness.wait(1)
        assert not context.cancelled

    runtime, graph, _, _ = _runtime(monkeypatch, readiness=readiness)
    starter, errors = _call_in_thread(runtime.start)
    assert graph.pipeline.playing_requested.wait(1)
    assert readiness_entered.wait(1)
    _assert_state(runtime, PipelineState.STARTING)

    allow_readiness.set()
    starter.join(1)
    assert not starter.is_alive()
    assert errors == []
    _assert_state(runtime, PipelineState.RUNNING)

    runtime.stop()
    _assert_state(runtime, PipelineState.STOPPED)
    assert graph.released.is_set()


def test_start_succeeds_if_pipeline_completes_immediately_after_running(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, graph, _, _ = _runtime(monkeypatch)

    def complete(context: _WorkerContext) -> None:
        while runtime.state is PipelineState.STARTING:
            assert not context.wait_cancelled(0.001)
        context.stop()

    runtime.add_worker("immediate-completion", complete)
    runtime.start()

    deadline = time.monotonic() + 1
    while runtime.state is not PipelineState.STOPPED and time.monotonic() < deadline:
        time.sleep(0.001)
    assert runtime.state is PipelineState.STOPPED
    assert runtime.failure is None
    assert graph.released.is_set()


def test_start_requires_top_level_pipeline_state_message(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, graph, _, gst = _runtime(monkeypatch, announce_playing=False)
    starter, errors = _call_in_thread(runtime.start)
    assert graph.pipeline.playing_requested.wait(1)
    graph.pipeline.bus.push(FakeMessage(gst.MessageType.STATE_CHANGED, source=object()))
    _assert_state(runtime, PipelineState.STARTING)

    graph.pipeline.bus.push(FakeMessage(gst.MessageType.STATE_CHANGED, source=graph.pipeline))
    starter.join(1)
    assert errors == []
    _assert_state(runtime, PipelineState.RUNNING)
    runtime.stop()


def test_stop_during_startup_cancels_start_without_retained_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    readiness_entered = Event()

    def readiness(context: _WorkerContext) -> None:
        readiness_entered.set()
        assert context.wait_cancelled(1)

    runtime, graph, _, _ = _runtime(monkeypatch, readiness=readiness)
    starter, errors = _call_in_thread(runtime.start)
    assert readiness_entered.wait(1)

    runtime.stop(timeout=0.2)
    starter.join(1)
    assert len(errors) == 1
    assert isinstance(errors[0], PipelineStateError)
    assert runtime.failure is None
    _assert_state(runtime, PipelineState.STOPPED)
    assert graph.released.is_set()


def test_stop_during_graph_build_never_starts_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    build_entered = Event()
    release_build = Event()

    def build_graph(graph: _PipelineGraph) -> None:
        del graph
        build_entered.set()
        assert release_build.wait(1)

    runtime, graph, _, _ = _runtime(monkeypatch, build_graph=build_graph)
    starter, start_errors = _call_in_thread(runtime.start)
    assert build_entered.wait(1)
    stopper, stop_errors = _call_in_thread(runtime.stop)
    release_build.set()
    starter.join(1)
    stopper.join(1)

    assert len(start_errors) == 1
    assert isinstance(start_errors[0], PipelineStateError)
    assert stop_errors == []
    assert not graph.pipeline.playing_requested.is_set()
    assert graph.cleanup_complete


def test_stop_created_and_repeated_normal_stop_are_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, graph, _, _ = _runtime(monkeypatch)
    runtime.stop()
    runtime.stop()
    assert runtime.state is PipelineState.STOPPED
    assert not graph.released.is_set()
    with pytest.raises(PipelineStateError):
        runtime.start()


def test_concurrent_second_start_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, graph, _, _ = _runtime(monkeypatch, announce_playing=False)
    starter, errors = _call_in_thread(runtime.start)
    assert graph.pipeline.playing_requested.wait(1)
    with pytest.raises(PipelineStateError):
        runtime.start()
    runtime.stop()
    starter.join(1)
    assert len(errors) == 1
    assert isinstance(errors[0], PipelineStateError)


def test_concurrent_external_stops_join_one_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    hold_worker = Event()
    worker_entered = Event()
    runtime, graph, _, _ = _runtime(monkeypatch)

    def worker(context: _WorkerContext) -> None:
        worker_entered.set()
        while not context.cancelled:
            hold_worker.wait(0.01)

    runtime.add_worker("cooperative", worker)
    runtime.start()
    assert worker_entered.wait(1)
    first, first_errors = _call_in_thread(runtime.stop)
    second, second_errors = _call_in_thread(runtime.stop)
    first.join(1)
    second.join(1)
    assert first_errors == []
    assert second_errors == []
    assert graph.released.is_set()


@pytest.mark.parametrize("failure_mode", ["state", "readiness"])
def test_startup_failure_is_retained(monkeypatch: pytest.MonkeyPatch, failure_mode: str) -> None:
    expected = RuntimeError("not ready")

    def readiness(context: _WorkerContext) -> None:
        del context
        raise expected

    state_result = -1 if failure_mode == "state" else 0
    runtime, graph, _, gst = _runtime(
        monkeypatch, state_result=state_result, readiness=readiness if failure_mode == "readiness" else None
    )
    with pytest.raises(PipelineError) as raised:
        runtime.start()
    assert raised.value is runtime.failure
    assert runtime.state is PipelineState.STOPPED
    assert graph.released.is_set()
    if failure_mode == "state":
        assert isinstance(raised.value.__cause__, GStreamerElementError)
        assert gst.StateChangeReturn.FAILURE == -1
    else:
        assert raised.value.__cause__ is expected


def test_start_timeout_is_terminal_and_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, graph, _, _ = _runtime(monkeypatch, announce_playing=False)
    with pytest.raises(PipelineTimeoutError) as raised:
        runtime.start(timeout=0.05)
    assert raised.value is runtime.failure
    assert runtime.state is PipelineState.STOPPED
    assert graph.released.is_set()
    with pytest.raises(PipelineTimeoutError) as repeated:
        runtime.stop()
    assert repeated.value is raised.value


def test_worker_can_request_stop_without_self_join(monkeypatch: pytest.MonkeyPatch) -> None:
    trigger = Event()
    worker_returned = Event()
    runtime, _, _, _ = _runtime(monkeypatch)

    def worker(context: _WorkerContext) -> None:
        assert trigger.wait(1)
        context.stop()
        worker_returned.set()

    runtime.add_worker("callback", worker)
    runtime.start()
    trigger.set()
    assert worker_returned.wait(1)
    runtime.wait(timeout=1)
    assert runtime.state is PipelineState.STOPPED


def test_worker_failure_retains_original_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    trigger = Event()
    original = RuntimeError("worker exploded")
    runtime, _, _, _ = _runtime(monkeypatch)

    def worker(context: _WorkerContext) -> None:
        del context
        assert trigger.wait(1)
        raise original

    runtime.add_worker("delivery", worker)
    runtime.start()
    trigger.set()
    with pytest.raises(PipelineError) as raised:
        runtime.wait(timeout=1)
    assert raised.value is runtime.failure
    assert raised.value.__cause__ is original
    with pytest.raises(PipelineError) as stopped:
        runtime.stop()
    assert stopped.value is raised.value


def test_worker_failure_during_normal_cancellation_is_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    original = RuntimeError("on_stop failed")
    runtime, _, _, _ = _runtime(monkeypatch)

    def worker(context: _WorkerContext) -> None:
        assert context.wait_cancelled(1)
        raise original

    runtime.add_worker("callback", worker)
    runtime.start()
    with pytest.raises(PipelineError) as raised:
        runtime.stop()
    assert raised.value.__cause__ is original


def test_bus_warning_is_nonfatal_and_error_is_not_eos(monkeypatch: pytest.MonkeyPatch) -> None:
    eos_called = Mock()
    runtime, graph, logger, gst = _runtime(monkeypatch, on_eos=eos_called)
    runtime.start()
    graph.pipeline.bus.push(FakeMessage(gst.MessageType.WARNING, source=graph.pipeline, error="underrun"))
    error_message = FakeMessage(gst.MessageType.ERROR, source=graph.pipeline, error="device gone")
    graph.pipeline.bus.push(error_message)

    with pytest.raises(PipelineError) as raised:
        runtime.wait(timeout=1)
    assert raised.value.__cause__ is error_message.parsed_error
    assert eos_called.call_count == 0
    assert logger.warning.call_args == call("gstreamer_bus_warning", error="underrun", debug="warning debug")


def test_eos_performs_normal_stop_or_delegates_to_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, graph, _, gst = _runtime(monkeypatch)
    runtime.start()
    graph.pipeline.bus.push(FakeMessage(gst.MessageType.EOS, source=graph.pipeline))
    runtime.wait(timeout=1)
    assert runtime.failure is None

    eos_seen = Event()
    delegated, delegated_graph, _, delegated_gst = _runtime(monkeypatch, on_eos=eos_seen.set)
    delegated.start()
    delegated_graph.pipeline.bus.push(FakeMessage(delegated_gst.MessageType.EOS, source=delegated_graph.pipeline))
    assert eos_seen.wait(1)
    assert delegated.state is PipelineState.RUNNING
    delegated.stop()


def test_first_failure_survives_graph_cleanup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    cleanup = RuntimeError("null failed")
    runtime, graph, _, gst = _runtime(monkeypatch, release_errors=(cleanup,))
    runtime.start()
    graph.pipeline.bus.push(FakeMessage(gst.MessageType.ERROR, source=graph.pipeline, error="primary"))

    with pytest.raises(PipelineError) as raised:
        runtime.wait(timeout=1)
    assert str(raised.value.__cause__) == "primary"
    assert len(raised.value.secondary_errors) == 1
    assert raised.value.secondary_errors[0] is cleanup


def test_first_failure_survives_graph_finalization_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = RuntimeError("source failed")
    finalization = RuntimeError("encoder failed")
    trigger = Event()
    runtime, _, _, _ = _runtime(monkeypatch)
    cast(Any, runtime)._finalize_graph = Mock(side_effect=finalization)

    def worker(context: _WorkerContext) -> None:
        assert trigger.wait(1)
        context.fail("capture failed", primary)

    runtime.add_worker("failure", worker)
    runtime.start()
    trigger.set()

    with pytest.raises(PipelineError) as raised:
        runtime.wait(timeout=1)

    assert raised.value.__cause__ is primary
    assert raised.value.secondary_errors == (finalization,)


def test_finalizer_runs_before_close_and_timeout_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    timeout = PipelineTimeoutError("recording finalization timed out")
    runtime, graph, _, _ = _runtime(monkeypatch)
    expected_graph = cast(_PipelineGraph, graph)
    original_close = graph.close

    def close() -> tuple[BaseException, ...]:
        calls.append("close")
        return original_close()

    cast(Any, graph).close = close

    def finalize(owned: _PipelineGraph, deadline: float) -> None:
        assert owned is expected_graph
        assert deadline >= time.monotonic()
        calls.append("finalize")
        raise timeout

    cast(Any, runtime)._finalize_graph = finalize
    runtime.start()

    with pytest.raises(PipelineTimeoutError) as raised:
        runtime.stop()

    assert raised.value is timeout
    assert runtime.failure is timeout
    assert runtime.state is PipelineState.STOPPED
    assert calls[:2] == ["finalize", "close"]


def test_blocked_finalizer_is_forced_to_null_within_stop_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = Event()
    release = Event()
    runtime, graph, _, _ = _runtime(monkeypatch)

    def finalize(owned: _PipelineGraph, deadline: float) -> None:
        del owned, deadline
        entered.set()
        release.wait()

    cast(Any, runtime)._finalize_graph = finalize
    runtime.start()
    started = time.monotonic()
    try:
        with pytest.raises(PipelineTimeoutError):
            runtime.stop(timeout=0.02)
        assert time.monotonic() - started < 0.5
        assert runtime.state is PipelineState.STOPPED
        assert graph.released.is_set()
        assert entered.is_set()
    finally:
        release.set()
    deadline = time.monotonic() + 1
    while not graph.cleanup_complete and time.monotonic() < deadline:
        time.sleep(0.001)
    assert graph.cleanup_complete


def test_wait_timeout_is_non_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, _, _, _ = _runtime(monkeypatch)
    with pytest.raises(PipelineStateError):
        runtime.wait()
    runtime.start()
    with pytest.raises(PipelineTimeoutError):
        runtime.wait(timeout=0.02)
    assert runtime.state is PipelineState.RUNNING
    assert runtime.failure is None
    runtime.stop()


def test_non_cooperative_worker_makes_stop_timeout_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = Event()
    release_worker = Event()
    runtime, graph, _, _ = _runtime(monkeypatch)

    def worker(context: _WorkerContext) -> None:
        del context
        entered.set()
        release_worker.wait()

    runtime.add_worker("stuck-callback", worker)
    runtime.start()
    assert entered.wait(1)
    try:
        with pytest.raises(PipelineTimeoutError) as raised:
            runtime.stop(timeout=0.02)
        assert raised.value is runtime.failure
        assert runtime.state is PipelineState.STOPPED
        assert graph.released.is_set()
    finally:
        release_worker.set()


def test_worker_cannot_use_graph_after_release(monkeypatch: pytest.MonkeyPatch) -> None:
    continue_after_stop = Event()
    rejected = Event()
    runtime, graph, _, _ = _runtime(monkeypatch)

    def worker(context: _WorkerContext) -> None:
        assert continue_after_stop.wait(1)
        with pytest.raises(GStreamerElementError, match="unavailable|released"):
            context.use_graph(lambda pipeline: pipeline)
        rejected.set()

    runtime.add_worker("late", worker)
    runtime.start()
    stopper, errors = _call_in_thread(lambda: runtime.stop(timeout=0.5))
    assert graph.released.wait(1)
    continue_after_stop.set()
    assert rejected.wait(1)
    stopper.join(1)
    assert errors == []


def test_close_unblocks_an_inflight_graph_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    operation_entered = Event()
    runtime, graph, _, _ = _runtime(monkeypatch)

    def worker(context: _WorkerContext) -> None:
        def blocking_operation(pipeline: object) -> None:
            del pipeline
            operation_entered.set()
            assert graph.released.wait(1)

        context.use_graph(blocking_operation)

    runtime.add_worker("blocked-push", worker)
    runtime.start()
    assert operation_entered.wait(1)
    runtime.stop(timeout=0.5)
    _assert_state(runtime, PipelineState.STOPPED)


def test_partial_build_failure_is_cleaned_by_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    original = RuntimeError("link failed")
    seen_graph: list[_PipelineGraph] = []

    def build_graph(owned: _PipelineGraph) -> None:
        seen_graph.append(owned)
        raise original

    runtime, graph, _, _ = _runtime(monkeypatch, build_graph=build_graph)
    with pytest.raises(PipelineError) as raised:
        runtime.start()
    assert raised.value.__cause__ is original
    assert seen_graph == [cast(_PipelineGraph, graph)]
    assert graph.cleanup_complete


def test_worker_can_replace_graph_without_leaving_running_state(monkeypatch: pytest.MonkeyPatch) -> None:
    gst = _gst()
    monkeypatch.setattr(runtime_module, "get_gst", lambda: gst)
    first = FakeGraph(FakePipeline(gst))
    second = FakeGraph(FakePipeline(gst))
    graphs = iter((first, second))
    restarted = Event()
    states: list[PipelineState] = []
    runtime = _PipelineRuntime(
        logger=Mock(bind=Mock(return_value=Mock())),
        graph_factory=lambda: cast(_PipelineGraph, next(graphs)),
    )

    def recovery(context: _WorkerContext) -> None:
        while not restarted.wait(0.01):
            if context.cancelled:
                return
        context.restart_graph(lambda worker: states.append(runtime.state))

    runtime.add_worker("recovery", recovery)
    runtime.start()
    restarted.set()
    deadline = time.monotonic() + 1
    while not states and time.monotonic() < deadline:
        time.sleep(0.001)

    assert first.cleanup_complete
    assert states == [PipelineState.RUNNING]
    assert runtime.state is PipelineState.RUNNING
    runtime.stop()
    assert second.cleanup_complete


def test_second_restart_attempt_builds_a_fresh_graph_after_first_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    gst = _gst()
    monkeypatch.setattr(runtime_module, "get_gst", lambda: gst)
    initial = FakeGraph(FakePipeline(gst))
    failed = FakeGraph(FakePipeline(gst, state_result=gst.StateChangeReturn.FAILURE))
    recovered = FakeGraph(FakePipeline(gst))
    graphs = iter((initial, failed, recovered))
    trigger = Event()
    completed = Event()
    attempt_errors: list[BaseException] = []
    runtime = _PipelineRuntime(
        logger=Mock(bind=Mock(return_value=Mock())),
        graph_factory=lambda: cast(_PipelineGraph, next(graphs)),
    )

    def recovery(context: _WorkerContext) -> None:
        assert trigger.wait(1)
        try:
            context.restart_graph(lambda worker: None)
        except GStreamerElementError as error:
            attempt_errors.append(error)
        context.restart_graph(lambda worker: None)
        completed.set()

    runtime.add_worker("recovery", recovery)
    runtime.start()
    trigger.set()
    assert completed.wait(1)

    assert len(attempt_errors) == 1
    assert initial.cleanup_complete
    assert failed.cleanup_complete
    assert runtime.state is PipelineState.RUNNING
    runtime.stop()
    assert recovered.cleanup_complete


def test_stop_remains_bounded_while_replacement_factory_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    gst = _gst()
    monkeypatch.setattr(runtime_module, "get_gst", lambda: gst)
    initial = FakeGraph(FakePipeline(gst))
    replacement = FakeGraph(FakePipeline(gst))
    factory_entered = Event()
    release_factory = Event()
    calls = 0

    def graph_factory() -> _PipelineGraph:
        nonlocal calls
        calls += 1
        if calls == 1:
            return cast(_PipelineGraph, initial)
        factory_entered.set()
        assert release_factory.wait(1)
        return cast(_PipelineGraph, replacement)

    trigger = Event()
    runtime = _PipelineRuntime(logger=Mock(bind=Mock(return_value=Mock())), graph_factory=graph_factory)

    def recovery(context: _WorkerContext) -> None:
        assert trigger.wait(1)
        try:
            context.restart_graph(lambda worker: None)
        except PipelineStateError:
            return

    runtime.add_worker("recovery", recovery)
    runtime.start()
    trigger.set()
    assert factory_entered.wait(1)
    started = time.monotonic()
    try:
        with pytest.raises(PipelineTimeoutError):
            runtime.stop(timeout=0.05)
        assert time.monotonic() - started < 0.5
        assert runtime.state is PipelineState.STOPPED
    finally:
        release_factory.set()
    deadline = time.monotonic() + 1
    while not replacement.cleanup_complete and time.monotonic() < deadline:
        time.sleep(0.001)
    assert replacement.cleanup_complete


def test_handled_bus_error_aborts_active_restart_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    gst = _gst()
    monkeypatch.setattr(runtime_module, "get_gst", lambda: gst)
    initial = FakeGraph(FakePipeline(gst))
    replacement = FakeGraph(FakePipeline(gst, announce_playing=False))
    graphs = iter((initial, replacement))
    trigger = Event()
    attempt_finished = Event()
    errors: list[BaseException] = []
    runtime = _PipelineRuntime(
        logger=Mock(bind=Mock(return_value=Mock())),
        graph_factory=lambda: cast(_PipelineGraph, next(graphs)),
        on_bus_error=lambda source, cause: True,
    )

    def recovery(context: _WorkerContext) -> None:
        assert trigger.wait(1)
        try:
            context.restart_graph(lambda worker: None)
        except GStreamerElementError as error:
            errors.append(error)
        attempt_finished.set()

    runtime.add_worker("recovery", recovery)
    runtime.start()
    trigger.set()
    assert replacement.pipeline.playing_requested.wait(1)
    message = FakeMessage(gst.MessageType.ERROR, source=object(), error="replacement source failed")
    replacement.pipeline.bus.push(message)
    assert attempt_finished.wait(1)

    assert len(errors) == 1
    assert errors[0].__cause__ is message.parsed_error
    assert replacement.cleanup_complete
    assert runtime.state is PipelineState.RUNNING
    runtime.stop()


@pytest.mark.gstreamer(factories=("fakesrc", "fakesink"))
def test_runtime_controls_real_gstreamer_pipeline() -> None:
    from lumivox_devicelab._gstreamer.elements.base import BaseElement

    logger = Mock()
    logger.bind.return_value = logger

    def graph_factory() -> _PipelineGraph:
        return _PipelineGraph(logger=logger, name="runtime-test")

    def build_graph(graph: _PipelineGraph) -> None:
        source = BaseElement("fakesrc", "source")
        source.impl.set_property("is-live", True)
        sink = BaseElement("fakesink", "sink")
        graph.add(source, sink)
        graph.link(source, sink)

    runtime = _PipelineRuntime(logger=logger, graph_factory=graph_factory, build_graph=build_graph)
    runtime.start()
    _assert_state(runtime, PipelineState.RUNNING)
    runtime.stop()
    _assert_state(runtime, PipelineState.STOPPED)
