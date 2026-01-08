"""Internal lifecycle and supervision for one GStreamer pipeline."""

from __future__ import annotations

import time
from typing import Any, TypeVar
from threading import Lock, Event, Thread, Condition, current_thread
from collections.abc import Callable

from lumivox_core.logger import Logger

from lumivox_devicelab.state import PipelineState
from lumivox_devicelab.errors import PipelineError, PipelineStateError, PipelineTimeoutError
from lumivox_devicelab._validation import validate_timeout

from .graph import _PipelineGraph
from .runtime import GStreamerElementError, get_gst

_T = TypeVar("_T")
_WorkerTarget = Callable[["_WorkerContext"], None]
_RestartReadiness = Callable[["_WorkerContext"], _T]
_RestartCommit = Callable[[_T], None]
_BusErrorHandler = Callable[[object, BaseException], bool]

_BUS_POLL_NS = 100_000_000
_ASYNC_TEARDOWN_TIMEOUT = 5.0
_RESTART_STARTUP_TIMEOUT = 10.0
_GRAPH_IDLE_TIMEOUT = 1.0


class _FatalRestartError(GStreamerElementError):
    """A restart invariant or cleanup failure that must not be retried."""


class _WorkerContext:
    """Cancellation and guarded graph access supplied to supervised workers."""

    def __init__(self, runtime: _PipelineRuntime) -> None:
        self._runtime = runtime

    @property
    def cancelled(self) -> bool:
        return self._runtime._cancel_event.is_set()

    def wait_cancelled(self, timeout: float | None = None) -> bool:
        return self._runtime._cancel_event.wait(timeout)

    def wait_graph_closed(self, timeout: float | None = None) -> bool:
        """Wait until graph access is closed and the pipeline has entered NULL."""
        return self._runtime._graph_closed_event.wait(timeout)

    @property
    def failure(self) -> PipelineError | None:
        return self._runtime.failure

    def use_graph(self, operation: Callable[[Any], _T]) -> _T:
        graph = self._runtime._get_graph()
        return graph.use(operation)

    def stop(self) -> None:
        """Request normal shutdown without waiting for the calling worker."""
        self._runtime.stop()

    def fail(self, message: str, cause: BaseException) -> PipelineError:
        """Record a worker failure immediately so final callbacks can observe it."""
        self._runtime._report_failure(message, cause)
        failure = self._runtime.failure
        assert failure is not None
        return failure

    def restart_graph(self, readiness: _RestartReadiness[_T], commit: _RestartCommit[_T] | None = None) -> _T:
        """Replace the current graph while preserving the public running state."""
        return self._runtime._restart_graph(self, readiness, commit)

    def begin_callback(self) -> bool:
        """Linearize a callback start against external stop."""
        with self._runtime._condition:
            return self._runtime._state is PipelineState.RUNNING and not self._runtime._cancel_event.is_set()

    def raise_if_restart_aborted(self) -> None:
        self._runtime._raise_if_restart_aborted()


class _PipelineRuntime:
    """Compose deterministic lifecycle around one single-use pipeline graph."""

    def __init__(
        self,
        *,
        logger: Logger,
        graph_factory: Callable[[], _PipelineGraph],
        build_graph: Callable[[_PipelineGraph], None] | None = None,
        readiness: _WorkerTarget | None = None,
        on_eos: Callable[[], None] | None = None,
        on_bus_error: _BusErrorHandler | None = None,
    ) -> None:
        self._logger = logger.bind(module="devicelab")
        self._graph_factory = graph_factory
        self._build_graph = build_graph
        self._readiness = readiness
        self._on_eos = on_eos
        self._on_bus_error = on_bus_error

        self._condition = Condition(Lock())
        self._state = PipelineState.CREATED
        self._failure: PipelineError | None = None
        self._graph: _PipelineGraph | None = None
        self._graph_pipeline: object | None = None
        self._retired_graphs: list[_PipelineGraph] = []
        self._worker_targets: list[tuple[str, _WorkerTarget]] = []
        self._workers: list[Thread] = []
        self._worker_ids: set[int] = set()
        self._control_thread: Thread | None = None
        self._cancel_event = Event()
        self._playing_event = Event()
        self._readiness_event = Event()
        self._graph_closed_event = Event()
        self._teardown_deadline: float | None = None
        self._restart_in_progress = False
        self._restart_error: BaseException | None = None
        self._restart_error_event = Event()

    @property
    def state(self) -> PipelineState:
        with self._condition:
            return self._state

    @property
    def failure(self) -> PipelineError | None:
        with self._condition:
            return self._failure

    def add_worker(self, name: str, target: _WorkerTarget) -> None:
        """Register a pipeline worker before startup."""
        if not name:
            raise ValueError("worker name must not be empty")
        with self._condition:
            if self._state is not PipelineState.CREATED:
                raise PipelineStateError("workers can only be registered before pipeline startup")
            self._worker_targets.append((name, target))

    def start(self, *, timeout: float = 10.0) -> None:
        validated = validate_timeout(timeout)
        assert validated is not None
        deadline = time.monotonic() + validated
        with self._condition:
            if self._state is not PipelineState.CREATED:
                raise PipelineStateError(f"cannot start pipeline in state {self._state.value}")
            self._state = PipelineState.STARTING
            control = Thread(target=self._supervise, name="devicelab-pipeline-control", daemon=False)
            self._control_thread = control
            control.start()

            while self._state not in (PipelineState.RUNNING, PipelineState.STOPPED):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)

            if self._state is PipelineState.RUNNING:
                return
            if self._state is PipelineState.STOPPED:
                if self._failure is not None:
                    raise self._failure
                raise PipelineStateError("pipeline startup was cancelled")

        timeout_error = PipelineTimeoutError("pipeline start timed out")
        self._force_terminal(timeout_error)
        raise self._authoritative_failure(timeout_error)

    def stop(self, *, timeout: float = 5.0) -> None:
        validated = validate_timeout(timeout)
        assert validated is not None
        deadline = time.monotonic() + validated
        calling_worker = self._is_worker_thread()

        with self._condition:
            if self._state is PipelineState.CREATED:
                self._state = PipelineState.STOPPED
                self._condition.notify_all()
                return
            if self._state is PipelineState.STOPPED:
                graph = self._graph
                already_stopped = True
            else:
                graph = None
                already_stopped = False

            if already_stopped:
                if calling_worker:
                    return
            else:
                self._request_stop_locked(deadline)
                if calling_worker:
                    return

                if self._wait_stopped_locked(deadline):
                    if self._failure is not None:
                        raise self._failure
                    return

        if already_stopped:
            if graph is not None:
                self._release_graph(graph)
            self._release_retired_graphs()
            failure = self.failure
            if failure is not None:
                raise failure
            return

        timeout_error = PipelineTimeoutError("pipeline stop timed out")
        self._force_terminal(timeout_error)
        failure = self._authoritative_failure(timeout_error)
        raise failure

    def wait(self, *, timeout: float | None = None) -> None:
        validated = validate_timeout(timeout, allow_none=True)
        deadline = None if validated is None else time.monotonic() + validated
        with self._condition:
            if self._state is PipelineState.CREATED:
                raise PipelineStateError("cannot wait for a pipeline that has not been started")
            while self._state is not PipelineState.STOPPED:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PipelineTimeoutError("pipeline wait timed out")
                self._condition.wait(remaining)
            if self._failure is not None:
                raise self._failure

    def _supervise(self) -> None:
        try:
            graph = self._graph_factory()
            with self._condition:
                if self._state is PipelineState.STOPPED:
                    release_immediately = True
                else:
                    self._graph = graph
                    self._graph_pipeline = graph.use(lambda pipeline: pipeline)
                    release_immediately = False
            if release_immediately:
                self._release_graph(graph)
                return

            if self._cancel_event.is_set():
                return
            if self._build_graph is not None:
                self._build_graph(graph)
            if self._cancel_event.is_set():
                return

            self._launch_worker("bus", self._monitor_bus)
            for name, target in self._worker_targets:
                self._launch_worker(name, target)

            if self._cancel_event.is_set():
                return
            gst = get_gst()
            result = graph.use(lambda pipeline: pipeline.set_state(gst.State.PLAYING))
            if result == gst.StateChangeReturn.FAILURE:
                raise GStreamerElementError("Failed to set GStreamer pipeline to PLAYING")

            self._wait_for_startup_signal(self._playing_event)
            if not self._cancel_event.is_set():
                if self._readiness is None:
                    self._readiness_event.set()
                else:
                    self._launch_worker("readiness", self._run_readiness)
                self._wait_for_startup_signal(self._readiness_event)

            with self._condition:
                if self._state is PipelineState.STARTING and not self._cancel_event.is_set():
                    self._state = PipelineState.RUNNING
                    self._condition.notify_all()

            self._cancel_event.wait()
        except Exception as error:
            self._report_failure("pipeline startup failed", error)
        finally:
            self._teardown()

    def _wait_for_startup_signal(self, signal: Event, deadline: float | None = None) -> None:
        while not signal.wait(0.05):
            if self._cancel_event.is_set():
                return
            self._raise_if_restart_aborted()
            if deadline is not None and time.monotonic() >= deadline:
                raise GStreamerElementError("restarted GStreamer pipeline startup timed out")

    def _run_readiness(self, context: _WorkerContext) -> None:
        assert self._readiness is not None
        self._readiness(context)
        self._readiness_event.set()

    def _monitor_bus(self, context: _WorkerContext) -> None:
        gst = get_gst()
        message_types = (
            gst.MessageType.ERROR | gst.MessageType.WARNING | gst.MessageType.EOS | gst.MessageType.STATE_CHANGED
        )
        while not context.cancelled:
            try:
                observed_pipeline, message = context.use_graph(
                    lambda pipeline: (
                        pipeline,
                        pipeline.get_bus().timed_pop_filtered(_BUS_POLL_NS, message_types),
                    )
                )
            except GStreamerElementError:
                if context.wait_cancelled(0):
                    return
                raise
            if message is None:
                continue
            with self._condition:
                if observed_pipeline is not self._graph_pipeline:
                    continue
            if message.type == gst.MessageType.WARNING:
                warning, debug = message.parse_warning()
                self._logger.warning("gstreamer_bus_warning", error=str(warning), debug=debug)
            elif message.type == gst.MessageType.ERROR:
                error, debug = message.parse_error()
                self._logger.error("gstreamer_bus_error", error=str(error), debug=debug)
                cause = error if isinstance(error, BaseException) else RuntimeError(str(error))
                if self._on_bus_error is not None and self._on_bus_error(message.src, cause):
                    self._abort_restart(cause)
                    continue
                self._report_failure("GStreamer pipeline bus error", cause)
                return
            elif message.type == gst.MessageType.EOS:
                if self._on_eos is None:
                    context.stop()
                else:
                    self._on_eos()
                    self._abort_restart(GStreamerElementError("pipeline reached EOS during restart"))
            elif message.type == gst.MessageType.STATE_CHANGED:
                old, new, pending = message.parse_state_changed()
                del old, pending
                if message.src is observed_pipeline and new == gst.State.PLAYING:
                    self._playing_event.set()

    def _launch_worker(self, name: str, target: _WorkerTarget) -> None:
        def run() -> None:
            ident = current_thread().ident
            assert ident is not None
            with self._condition:
                self._worker_ids.add(ident)
            try:
                target(_WorkerContext(self))
            except Exception as error:
                self._report_failure(f"pipeline worker '{name}' failed", error)
            finally:
                with self._condition:
                    self._worker_ids.discard(ident)
                    self._condition.notify_all()

        worker = Thread(target=run, name=f"devicelab-{name}", daemon=False)
        with self._condition:
            if self._cancel_event.is_set() or self._state is PipelineState.STOPPED:
                return
            self._workers.append(worker)
            worker.start()

    def _report_failure(self, message: str, cause: BaseException) -> None:
        with self._condition:
            if self._failure is None:
                error = cause if isinstance(cause, PipelineError) else PipelineError(message, cause=cause)
                self._failure = error
            elif cause is not self._failure:
                self._failure._add_secondary_error(cause)
            self._request_stop_locked(time.monotonic() + _ASYNC_TEARDOWN_TIMEOUT)

    def report_failure(self, message: str, cause: BaseException) -> None:
        """Report a fatal error from a non-worker backend callback."""
        self._report_failure(message, cause)

    def abort_restart(self, cause: BaseException) -> bool:
        """Abort only an active restart attempt, returning whether one existed."""
        return self._abort_restart(cause)

    def _abort_restart(self, cause: BaseException) -> bool:
        with self._condition:
            if not self._restart_in_progress:
                return False
            if self._restart_error is None:
                self._restart_error = cause
                self._restart_error_event.set()
            self._condition.notify_all()
            return True

    def _raise_if_restart_aborted(self) -> None:
        with self._condition:
            error = self._restart_error
        if error is not None:
            raise GStreamerElementError("restarted GStreamer pipeline failed") from error

    def _complete_restart_attempt(self, result: _T, commit: _RestartCommit[_T] | None) -> None:
        with self._condition:
            error = self._restart_error
            if error is None:
                if commit is not None:
                    commit(result)
                self._restart_in_progress = False
        if error is not None:
            raise GStreamerElementError("restarted GStreamer pipeline failed") from error

    def _restart_graph(
        self,
        worker: _WorkerContext,
        readiness: _RestartReadiness[_T],
        commit: _RestartCommit[_T] | None,
    ) -> _T:
        with self._condition:
            if self._state is not PipelineState.RUNNING or self._cancel_event.is_set():
                raise PipelineStateError("microphone recovery was cancelled")
            old_graph = self._graph
            self._graph = None
            self._graph_pipeline = None
            self._playing_event.clear()
            self._restart_error = None
            self._restart_error_event.clear()
            self._restart_in_progress = True
            self._condition.notify_all()

        try:
            if old_graph is not None:
                self._release_for_restart(old_graph)
            if worker.cancelled:
                raise PipelineStateError("microphone recovery was cancelled")
            graph = self._graph_factory()
        except Exception:
            with self._condition:
                self._restart_in_progress = False
                self._restart_error = None
                self._restart_error_event.clear()
            raise
        try:
            if worker.cancelled:
                raise PipelineStateError("microphone recovery was cancelled")
            if self._build_graph is not None:
                self._build_graph(graph)
            if worker.cancelled:
                raise PipelineStateError("microphone recovery was cancelled")
            pipeline = graph.use(lambda pipeline: pipeline)
            with self._condition:
                if self._state is not PipelineState.RUNNING or self._cancel_event.is_set():
                    raise PipelineStateError("microphone recovery was cancelled")
                self._graph = graph
                self._graph_pipeline = pipeline
                self._condition.notify_all()
            gst = get_gst()
            result = graph.use(lambda pipeline: pipeline.set_state(gst.State.PLAYING))
            if result == gst.StateChangeReturn.FAILURE:
                raise GStreamerElementError("Failed to set restarted GStreamer pipeline to PLAYING")
            deadline = time.monotonic() + _RESTART_STARTUP_TIMEOUT
            self._wait_for_startup_signal(self._playing_event, deadline)
            if worker.cancelled:
                raise PipelineStateError("microphone recovery was cancelled")
            result_value = readiness(worker)
            self._complete_restart_attempt(result_value, commit)
            return result_value
        except Exception:
            with self._condition:
                if self._graph is graph:
                    self._graph = None
                    self._graph_pipeline = None
                    self._condition.notify_all()
            self._release_failed_restart(graph)
            raise
        finally:
            with self._condition:
                self._restart_in_progress = False
                self._restart_error = None
                self._restart_error_event.clear()

    def _release_for_restart(self, graph: _PipelineGraph) -> None:
        errors = graph.close()
        if not graph.wait_idle(_GRAPH_IDLE_TIMEOUT):
            self._retire_graph(graph)
            raise _FatalRestartError("previous GStreamer graph did not become idle")
        errors = (*errors, *graph.release())
        if not graph.cleanup_complete:
            self._retire_graph(graph)
        if errors:
            raise _FatalRestartError("failed to release the previous GStreamer graph") from errors[0]

    def _release_failed_restart(self, graph: _PipelineGraph) -> None:
        errors = graph.close()
        idle = graph.wait_idle(_GRAPH_IDLE_TIMEOUT)
        if idle:
            errors = (*errors, *graph.release())
        if not graph.cleanup_complete:
            self._retire_graph(graph)
        if not idle:
            raise _FatalRestartError("replacement GStreamer graph did not become idle")
        if errors:
            raise _FatalRestartError("failed to release a replacement GStreamer graph") from errors[0]

    def _request_stop_locked(self, deadline: float) -> None:
        if self._state in (PipelineState.STARTING, PipelineState.RUNNING):
            self._state = PipelineState.STOPPING
        if self._teardown_deadline is None or deadline < self._teardown_deadline:
            self._teardown_deadline = deadline
        self._cancel_event.set()
        self._condition.notify_all()

    def _teardown(self) -> None:
        self._cancel_event.set()
        with self._condition:
            if self._state is PipelineState.STOPPED:
                return
            if self._state in (PipelineState.STARTING, PipelineState.RUNNING):
                self._state = PipelineState.STOPPING
            deadline = self._teardown_deadline or (time.monotonic() + _ASYNC_TEARDOWN_TIMEOUT)
        with self._condition:
            graph = self._graph
            self._graph = None
            self._graph_pipeline = None
        if graph is not None:
            self._close_graph(graph)
        else:
            self._graph_closed_event.set()
        self._join_workers(deadline)
        if graph is not None:
            self._release_graph(graph)
        self._release_retired_graphs()
        self._complete_stopped()

    def _force_terminal(self, timeout_error: PipelineTimeoutError) -> None:
        with self._condition:
            self._record_failure_locked(timeout_error)
            self._request_stop_locked(time.monotonic())
        with self._condition:
            graph = self._graph
            self._graph = None
            self._graph_pipeline = None
        if graph is not None:
            self._close_graph(graph)
        else:
            self._graph_closed_event.set()
        self._join_workers(time.monotonic())
        if graph is not None:
            self._release_graph(graph)
        self._release_retired_graphs()
        self._complete_stopped()

    def _close_graph(self, graph: _PipelineGraph) -> None:
        try:
            errors = graph.close()
        except Exception as close_error:
            errors = (close_error,)
        self._record_cleanup_errors(errors)
        self._graph_closed_event.set()

    def _release_graph(self, graph: _PipelineGraph) -> None:
        try:
            errors = graph.release()
        except Exception as release_error:
            errors = (release_error,)
        self._record_cleanup_errors(errors)
        if graph.cleanup_complete:
            with self._condition:
                if self._graph is graph:
                    self._graph = None

    def _record_cleanup_errors(self, errors: tuple[BaseException, ...]) -> None:
        with self._condition:
            for error in errors:
                if self._failure is None:
                    self._failure = PipelineError("pipeline graph cleanup failed", cause=error)
                else:
                    self._failure._add_secondary_error(error)

    def _release_retired_graphs(self) -> None:
        with self._condition:
            graphs = tuple(self._retired_graphs)
            self._retired_graphs.clear()
        for graph in graphs:
            self._release_graph(graph)

    def _retire_graph(self, graph: _PipelineGraph) -> None:
        with self._condition:
            stopped = self._state is PipelineState.STOPPED
            if not stopped:
                self._retired_graphs.append(graph)
                return
        if graph.wait_idle(_ASYNC_TEARDOWN_TIMEOUT):
            self._release_graph(graph)
        elif not graph.cleanup_complete:
            with self._condition:
                self._retired_graphs.append(graph)

    def _join_workers(self, deadline: float) -> None:
        caller = current_thread()
        with self._condition:
            workers = tuple(self._workers)
        for worker in workers:
            if worker is caller:
                continue
            worker.join(max(0.0, deadline - time.monotonic()))
        alive = tuple(worker.name for worker in workers if worker is not caller and worker.is_alive())
        if alive:
            error = PipelineTimeoutError(f"pipeline workers did not stop: {', '.join(alive)}")
            with self._condition:
                self._record_failure_locked(error)

    def _complete_stopped(self) -> None:
        with self._condition:
            self._state = PipelineState.STOPPED
            self._condition.notify_all()

    def _record_failure_locked(self, error: PipelineError) -> None:
        if self._failure is None:
            self._failure = error
        elif error is not self._failure:
            self._failure._add_secondary_error(error)

    def _authoritative_failure(self, fallback: PipelineError) -> PipelineError:
        with self._condition:
            return self._failure or fallback

    def _get_graph(self) -> _PipelineGraph:
        with self._condition:
            while self._graph is None and not self._cancel_event.is_set():
                self._condition.wait(0.05)
            graph = self._graph
        if graph is None:
            raise GStreamerElementError("GStreamer graph is unavailable")
        return graph

    def _is_worker_thread(self) -> bool:
        ident = current_thread().ident
        with self._condition:
            return ident is not None and ident in self._worker_ids

    def _wait_stopped_locked(self, deadline: float) -> bool:
        while self._state is not PipelineState.STOPPED:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._condition.wait(remaining)
        return True
