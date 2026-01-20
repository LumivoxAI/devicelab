"""PipeWire speaker playback pipeline."""

from __future__ import annotations

import time
from os import PathLike
from pathlib import Path
from threading import Lock, Event, RLock

import numpy as np
from lumivox_core.logger import Logger

from lumivox_devicelab.state import PipelineState
from lumivox_devicelab.errors import (
    PipelineError,
    PipelineStateError,
    PipelineTimeoutError,
    PlaybackSubmissionError,
)
from lumivox_devicelab.formats import AudioFormat, build_raw_audio_spec

from ._gstreamer.audio import validate_pcm_array
from ._gstreamer.graph import _PipelineGraph
from ._gstreamer.runtime import GStreamerElementError, get_gst
from ._gstreamer.recording import _RecordingBranch, validate_recording_path
from ._gstreamer.elements.app import AppSrc, PlaybackPushResult
from ._gstreamer.elements.base import BaseElement
from ._gstreamer.elements.flow import Tee, AudioQueue, QueueOverflowPolicy
from ._gstreamer.elements.audio import CapsFilter, AudioConvert, AudioResample
from ._gstreamer.device_discovery import DeviceDirection, resolve_pipewire_target
from ._gstreamer.pipeline_runtime import _WorkerContext, _PipelineRuntime
from ._gstreamer.elements.pipewire import PipeWireSink

_PLAYBACK_BRANCH_QUEUE_TIME_MS = 250


class SpeakerPlaybackPipeline:
    """Submit normalized PCM to one explicitly selected PipeWire speaker.

    A caller must leave each input array unchanged until ``submit`` returns.
    """

    def __init__(
        self,
        *,
        logger: Logger,
        audio_format: AudioFormat,
        device_id: str,
        record_to: str | PathLike[str] | None = None,
        overwrite: bool = False,
    ) -> None:
        if not isinstance(audio_format, AudioFormat):
            raise TypeError("audio_format must be an AudioFormat")
        if not isinstance(device_id, str):
            raise TypeError("device_id must be a string")
        if not device_id:
            raise ValueError("device_id must not be empty")
        if not isinstance(overwrite, bool):
            raise TypeError("overwrite must be a bool")
        if record_to is None:
            if overwrite:
                raise ValueError("overwrite=True requires record_to")
            recording_path = None
        else:
            recording_path = validate_recording_path(record_to, overwrite=overwrite)

        self._logger = logger.bind(module="devicelab")
        self._device_id = device_id
        self._spec = build_raw_audio_spec(audio_format)
        self._recording_path: Path | None = recording_path
        self._overwrite = overwrite
        self._target_object: str | None = None
        self._app_src: AppSrc | None = None
        self._recordings: dict[_PipelineGraph, _RecordingBranch] = {}
        self._recording_lock = RLock()
        self._operation_lock = Lock()
        self._eos = Event()

        self._runtime = _PipelineRuntime(
            logger=self._logger,
            graph_factory=self._create_graph,
            build_graph=self._build_graph,
            readiness=self._ready,
            on_eos=self._eos.set,
            finalize_graph=self._abort_recording,
            close_before_finalize=True,
        )

    @property
    def state(self) -> PipelineState:
        return self._runtime.state

    @property
    def failure(self) -> PipelineError | None:
        return self._runtime.failure

    def start(self, *, timeout: float = 10.0) -> None:
        self._runtime.start(timeout=timeout)

    def stop(self, *, immediate: bool = False, timeout: float = 10.0) -> None:
        if not isinstance(immediate, bool):
            raise TypeError("immediate must be a bool")
        if immediate:
            self._runtime.stop(timeout=timeout)
        else:
            self._stop_gracefully(timeout=timeout)

    def wait(self, *, timeout: float | None = None) -> None:
        self._runtime.wait(timeout=timeout)

    def submit(self, data: np.ndarray) -> None:
        """Append PCM, blocking for bounded AppSrc capacity when necessary."""
        validate_pcm_array(data, self._spec)
        with self._operation_lock:
            failure = self.failure
            if failure is not None:
                raise failure
            if self.state is not PipelineState.RUNNING:
                raise PipelineStateError(f"cannot submit playback in state {self.state.value}")
            app_src = self._app_src
            if app_src is None:
                raise PipelineStateError("playback AppSrc is unavailable")
            try:
                result = self._runtime.use_graph(
                    lambda pipeline: app_src.push(data, lambda: self._running_time_ns(pipeline))
                )
            except Exception as error:
                self._raise_submission_error(PlaybackPushResult(None, 0, error))
                raise AssertionError("unreachable")
            if result.error is not None or result.flow_return != get_gst().FlowReturn.OK:
                self._raise_submission_error(result)

    def _stop_gracefully(self, *, timeout: float) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be a number")
        if timeout <= 0 or not np.isfinite(timeout):
            raise ValueError("timeout must be positive and finite")
        deadline = time.monotonic() + float(timeout)
        if not self._operation_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            timeout_error = PipelineTimeoutError("pipeline stop timed out waiting for active submission")
            self._runtime.force_terminal(timeout_error)
            raise self.failure or timeout_error
        try:
            state = self.state
            if state is PipelineState.STOPPED:
                failure = self.failure
                if failure is not None:
                    raise failure
                return
            if state in (PipelineState.CREATED, PipelineState.STARTING):
                self._runtime.stop(timeout=max(0.0, deadline - time.monotonic()))
                return
            self._runtime.begin_graceful_stop()
            app_src = self._app_src
            if app_src is None:
                raise PipelineStateError("playback AppSrc is unavailable")
            try:
                flow_return = self._runtime.use_graph(lambda pipeline: app_src.push_eos())
            except Exception as error:
                self._raise_graceful_stop_interruption(error)
                return
            if flow_return != get_gst().FlowReturn.OK:
                self._raise_graceful_stop_interruption(GStreamerElementError(f"AppSrc EOS failed: {flow_return}"))
                return

            while not self._eos.wait(0.01):
                failure = self.failure
                if failure is not None:
                    raise failure
                if self.state is PipelineState.STOPPED:
                    raise PipelineStateError("graceful playback stop was interrupted")
                if time.monotonic() >= deadline:
                    timeout_error = PipelineTimeoutError("pipeline graceful stop timed out")
                    self._runtime.force_terminal(timeout_error)
                    raise self.failure or timeout_error

            try:
                self._complete_recording(deadline)
            except Exception as error:
                self._runtime.report_failure("speaker recording finalization failed", error)
                raise self.failure or error
            self._runtime.complete_graceful_stop(deadline, PipelineTimeoutError("pipeline graceful stop timed out"))
        finally:
            self._operation_lock.release()

    def _create_graph(self) -> _PipelineGraph:
        self._target_object = resolve_pipewire_target(self._device_id, DeviceDirection.SPEAKER, self._logger)
        return _PipelineGraph(logger=self._logger, name="speaker-playback")

    def _build_graph(self, graph: _PipelineGraph) -> None:
        if self._target_object is None:
            raise GStreamerElementError("speaker target has not been resolved")
        app_src = AppSrc(self._spec, name="playback-source")
        elements: list[BaseElement] = [
            app_src,
            AudioConvert(self._spec, name="convert"),
            AudioResample(name="resample"),
            CapsFilter(self._spec, name="normalized-caps"),
        ]
        sink = PipeWireSink(
            target_object=self._target_object,
            client_name="lumivox-devicelab",
            name="speaker-sink",
        )
        if self._recording_path is None:
            elements.append(sink)
            graph.add(*elements)
            graph.link(*elements)
        else:
            tee = Tee(name="normalized-tee")
            playback_queue = AudioQueue(
                max_time_ms=_PLAYBACK_BRANCH_QUEUE_TIME_MS,
                overflow_policy=QueueOverflowPolicy.BLOCK,
                name="playback-queue",
            )
            elements.append(tee)
            graph.add(*elements, playback_queue, sink)
            graph.link(*elements)
            graph.branch(tee, playback_queue, sink)
            recording = _RecordingBranch.build(
                graph=graph,
                tee=tee,
                logger=self._logger,
                path=self._recording_path,
                overwrite=self._overwrite,
            )
            with self._recording_lock:
                self._recordings[graph] = recording
        self._app_src = app_src

    def _ready(self, worker: _WorkerContext) -> None:
        del worker
        with self._recording_lock:
            for recording in self._recordings.values():
                recording.mark_active()

    @staticmethod
    def _running_time_ns(pipeline: object) -> int:
        clock = pipeline.get_clock()  # type: ignore[attr-defined]
        if clock is None:
            raise GStreamerElementError("playback pipeline has no clock")
        current = clock.get_time()
        base = pipeline.get_base_time()  # type: ignore[attr-defined]
        gst = get_gst()
        if current == gst.CLOCK_TIME_NONE or base == gst.CLOCK_TIME_NONE or current < base:
            raise GStreamerElementError("playback pipeline running time is unavailable")
        return int(current - base)

    def _raise_submission_error(self, result: PlaybackPushResult) -> None:
        failure = self.failure
        if failure is not None:
            reason: BaseException = failure
        elif self.state is not PipelineState.RUNNING:
            reason = PipelineStateError("playback submission was interrupted by stop")
        else:
            cause = result.error or GStreamerElementError(f"AppSrc push failed: {result.flow_return}")
            self._runtime.report_failure("playback AppSrc push failed", cause)
            reason = self.failure or cause
        raise PlaybackSubmissionError(
            "playback submission accepted only a frame prefix",
            accepted_frames=result.accepted_frames,
            cause=reason,
        )

    def _raise_graceful_stop_interruption(self, cause: BaseException) -> None:
        failure = self.failure
        if failure is not None:
            raise failure
        if self._runtime.cancelled:
            raise PipelineStateError("graceful playback stop was interrupted", cause=cause)
        self._runtime.report_failure("playback EOS failed", cause)
        raise self.failure or PipelineError("playback EOS failed", cause=cause)

    def _complete_recording(self, deadline: float) -> None:
        with self._recording_lock:
            recordings = tuple(self._recordings.values())
        for recording in recordings:
            recording.complete_source_eos(deadline)

    def _abort_recording(self, graph: _PipelineGraph, deadline: float) -> None:
        del deadline
        with self._recording_lock:
            recording = self._recordings.pop(graph, None)
        if recording is not None:
            recording.abort()
