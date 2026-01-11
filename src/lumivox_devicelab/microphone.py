"""PipeWire microphone capture pipeline.

This module remains outside the package exports until the complete v1 pipeline
API is released.
"""

from __future__ import annotations

import time
from os import PathLike
from pathlib import Path
from threading import Event, RLock

from lumivox_core.logger import Logger

from lumivox_devicelab.state import PipelineState
from lumivox_devicelab.errors import PipelineError
from lumivox_devicelab.capture import CaptureContext, CaptureHandler
from lumivox_devicelab.formats import AudioFormat, ChannelSelection, build_raw_audio_spec

from ._gstreamer.graph import _PipelineGraph
from ._gstreamer.runtime import GStreamerElementError
from ._gstreamer.recording import _RecordingError, _RecordingBranch, recording_segment_path, validate_recording_path
from ._gstreamer.elements.app import AppSink, AppSinkPolicy, CapturePacket, CapturePacketError
from ._gstreamer.elements.base import BaseElement
from ._gstreamer.elements.flow import Tee, AudioQueue, QueueOverflowPolicy
from ._gstreamer.elements.audio import CapsFilter, AudioConvert, AudioResample, SourceChannelCapsFilter
from ._gstreamer.capture_delivery import _CaptureDelivery, calibrate_capture_context
from ._gstreamer.capture_recovery import _CaptureHealth, _RestartBudget
from ._gstreamer.device_discovery import DeviceDirection, resolve_pipewire_target
from ._gstreamer.pipeline_runtime import _WorkerContext, _PipelineRuntime, _FatalRestartError
from ._gstreamer.elements.pipewire import PipeWireSrc

_CAPTURE_QUEUE_TIME_MS = 200
_RESTART_READINESS_TIMEOUT = 10.0


class MicrophoneCapturePipeline:
    """Capture normalized PCM from one explicitly selected PipeWire microphone."""

    def __init__(
        self,
        *,
        logger: Logger,
        handler: CaptureHandler,
        audio_format: AudioFormat,
        device_id: str,
        channel_selection: ChannelSelection | None = None,
        record_to: str | PathLike[str] | None = None,
        overwrite: bool = False,
    ) -> None:
        if not isinstance(handler, CaptureHandler):
            raise TypeError("handler must be a CaptureHandler")
        if not isinstance(audio_format, AudioFormat):
            raise TypeError("audio_format must be an AudioFormat")
        if not isinstance(device_id, str):
            raise TypeError("device_id must be a string")
        if not device_id:
            raise ValueError("device_id must not be empty")
        if channel_selection is not None and not isinstance(channel_selection, ChannelSelection):
            raise TypeError("channel_selection must be a ChannelSelection or None")
        if not isinstance(overwrite, bool):
            raise TypeError("overwrite must be a bool")
        if record_to is None:
            if overwrite:
                raise ValueError("overwrite=True requires record_to")
            recording_path = None
        else:
            recording_path = validate_recording_path(record_to, overwrite=overwrite)

        self._logger = logger.bind(module="devicelab")
        self._audio_format = audio_format
        self._device_id = device_id
        self._spec = build_raw_audio_spec(audio_format, channel_selection)
        self._channel_selection = channel_selection
        self._recording_path: Path | None = recording_path
        self._overwrite = overwrite
        self._building_generation = 0
        self._recordings: dict[_PipelineGraph, _RecordingBranch] = {}
        self._recording_lock = RLock()
        self._target_object: str | None = None
        self._app_sink: AppSink | None = None
        self._source_caps: SourceChannelCapsFilter | None = None
        self._pipewire_source: PipeWireSrc | None = None
        self._generation = 0
        self._health = _CaptureHealth()
        self._restart_budget = _RestartBudget()
        self._recovery_lock = RLock()
        self._recovery_event = Event()
        self._recovery_cause: PipelineError | None = None
        self._recovering = False

        self._delivery = _CaptureDelivery(
            logger=self._logger,
            handler=handler,
            pull_packet=self._pull_packet,
            health_observer=self._observe_packet_health,
        )
        self._runtime = _PipelineRuntime(
            logger=self._logger,
            graph_factory=self._create_graph,
            build_graph=self._build_graph,
            readiness=self._ready,
            on_eos=self._unexpected_eos,
            on_bus_error=self._handle_bus_error,
            finalize_graph=self._finalize_graph,
        )
        self._runtime.add_worker("capture-delivery", self._delivery.run)
        self._runtime.add_worker("capture-recovery", self._recover)

    @property
    def state(self) -> PipelineState:
        return self._runtime.state

    @property
    def failure(self) -> PipelineError | None:
        return self._runtime.failure

    def start(self, *, timeout: float = 10.0) -> None:
        self._runtime.start(timeout=timeout)

    def stop(self, *, timeout: float = 5.0) -> None:
        self._runtime.stop(timeout=timeout)

    def wait(self, *, timeout: float | None = None) -> None:
        self._runtime.wait(timeout=timeout)

    def _create_graph(self) -> _PipelineGraph:
        target = resolve_pipewire_target(self._device_id, DeviceDirection.MICROPHONE, self._logger)
        self._target_object = target
        return _PipelineGraph(logger=self._logger, name="microphone-capture")

    def _build_graph(self, graph: _PipelineGraph) -> None:
        if self._target_object is None:
            raise GStreamerElementError("microphone target has not been resolved")
        source = PipeWireSrc(target_object=self._target_object, client_name="lumivox-devicelab", name="source")
        convert = AudioConvert(self._spec, name="convert")
        resample = AudioResample(name="resample")
        normalized_caps = CapsFilter(self._spec, name="normalized-caps")
        capture_queue = AudioQueue(
            max_time_ms=_CAPTURE_QUEUE_TIME_MS,
            overflow_policy=QueueOverflowPolicy.DROP_OLD,
            name="capture-queue",
        )
        app_sink = AppSink(self._spec, name="capture-sink", policy=AppSinkPolicy.LIVE_DROP)
        app_sink.observe_caps(self._caps_changed)

        elements: list[BaseElement] = [source]
        source_caps = None
        if self._channel_selection is not None:
            source_caps = SourceChannelCapsFilter(self._channel_selection.source_channels, name="source-channels")
            elements.append(source_caps)
        elements.extend((convert, resample, normalized_caps))
        recording_path = self._recording_path
        if recording_path is None:
            elements.extend((capture_queue, app_sink))
            graph.add(*elements)
            graph.link(*elements)
        else:
            tee = Tee(name="normalized-tee")
            elements.append(tee)
            graph.add(*elements, capture_queue, app_sink)
            graph.link(*elements)
            graph.branch(tee, capture_queue, app_sink)
            segment_path = recording_segment_path(recording_path, self._building_generation)
            recording = _RecordingBranch.build(
                graph=graph,
                tee=tee,
                logger=self._logger,
                path=segment_path,
                overwrite=self._overwrite,
            )
            with self._recording_lock:
                self._recordings[graph] = recording
        self._source_caps = source_caps
        self._pipewire_source = source
        self._app_sink = app_sink

    def _ready(self, worker: _WorkerContext) -> None:
        while not worker.cancelled:
            source_caps = self._source_caps
            source_ready = source_caps is None or worker.use_graph(
                lambda pipeline: source_caps.validate_negotiated_caps()
            )
            sink = self._app_sink
            assert sink is not None
            sink_ready = worker.use_graph(lambda pipeline: sink.validate_negotiated_caps())
            if source_ready and sink_ready:
                context = self._calibrate(worker, generation=0)
                self._mark_recording_active()
                self._delivery.activate(worker, context)
                self._restart_budget.mark_running()
                return
            worker.wait_cancelled(0.01)

    def _pull_packet(self, timeout_ms: int) -> CapturePacket | None:
        sink = self._app_sink
        if sink is None:
            raise GStreamerElementError("microphone AppSink is unavailable")
        return sink.try_pull(timeout_ms)

    def _observe_packet_health(self, error: CapturePacketError) -> None:
        if self._health.observe(error):
            self._request_recovery("microphone capture data became unhealthy", error)

    def _caps_changed(self, error: CapturePacketError) -> None:
        if self.state is PipelineState.STARTING:
            self._runtime.report_failure("microphone initial caps are incompatible", error)
        elif self.state is PipelineState.RUNNING:
            self._request_recovery("microphone caps changed", error)

    def _handle_bus_error(self, source: object, cause: BaseException) -> bool:
        pipewire_source = self._pipewire_source
        if self.state is not PipelineState.RUNNING or pipewire_source is None or source is not pipewire_source.impl:
            return False
        self._request_recovery("microphone source failed", cause)
        return True

    def _unexpected_eos(self) -> None:
        if self.state is not PipelineState.RUNNING:
            raise GStreamerElementError("microphone pipeline reached unexpected EOS during startup")
        self._request_recovery(
            "microphone pipeline reached unexpected EOS",
            GStreamerElementError("microphone pipeline reached unexpected EOS"),
        )

    def _request_recovery(self, message: str, cause: BaseException) -> None:
        recovery_cause = cause if isinstance(cause, PipelineError) else PipelineError(message, cause=cause)
        with self._recovery_lock:
            recovering = self._recovering
        if recovering and self._runtime.abort_restart(cause):
            self._logger.warning("microphone_recovery_trigger_ignored", error=str(cause))
            return
        with self._recovery_lock:
            if self._recovery_cause is not None:
                self._logger.warning("microphone_recovery_trigger_ignored", error=str(cause))
                return
            self._recovery_cause = recovery_cause
            self._recovery_event.set()
            self._restart_budget.begin_recovery()
        self._delivery.force_discontinuity()
        self._logger.warning("microphone_recovery_requested", error=str(cause))

    def _recover(self, worker: _WorkerContext) -> None:
        while not worker.cancelled:
            if not self._recovery_event.wait(0.05):
                continue
            with self._recovery_lock:
                cause = self._recovery_cause
                self._recovery_cause = None
                self._recovery_event.clear()
                self._recovering = cause is not None
            if cause is None:
                continue
            if not self._delivery.pause(worker):
                return

            last_error: BaseException = cause
            while not worker.cancelled:
                delay = self._restart_budget.next_delay()
                if delay is None:
                    worker.fail("microphone recovery attempts exhausted", last_error)
                    return
                self._logger.warning("microphone_restart_scheduled", delay_seconds=delay, error=str(last_error))
                if worker.wait_cancelled(delay):
                    return
                try:
                    next_generation = self._generation + 1
                    self._building_generation = next_generation
                    completed_events: list[Event] = []
                    context = worker.restart_graph(
                        lambda restart_worker: self._wait_for_restart_ready(restart_worker, next_generation),
                        lambda restarted_context: completed_events.append(
                            self._commit_restart(restarted_context, cause)
                        ),
                    )
                except _FatalRestartError as error:
                    worker.fail("microphone restart cleanup failed", error)
                    return
                except _RecordingError as error:
                    worker.fail("microphone recording restart failed", error)
                    return
                except Exception as error:
                    if worker.wait_cancelled(0):
                        return
                    last_error = error
                    self._logger.warning("microphone_restart_failed", error=str(error))
                    continue

                del context
                if not completed_events:
                    raise RuntimeError("successful microphone restart was not committed")
                completed = completed_events[0]
                with self._recovery_lock:
                    self._recovering = False
                self._delivery.resume()
                while not completed.wait(0.01):
                    if worker.wait_cancelled(0):
                        return
                if worker.cancelled or worker.failure is not None:
                    return
                self._logger.info("microphone_restarted", generation=next_generation)
                break

    def _commit_restart(self, context: CaptureContext, cause: PipelineError) -> Event:
        completed = self._delivery.restart(context, cause)
        self._mark_recording_active()
        self._generation = context.generation
        self._health.clear()
        self._restart_budget.mark_running()
        return completed

    def _wait_for_restart_ready(self, worker: _WorkerContext, generation: int) -> CaptureContext:
        deadline = time.monotonic() + _RESTART_READINESS_TIMEOUT
        while not worker.cancelled:
            worker.raise_if_restart_aborted()
            source_caps = self._source_caps
            source_ready = source_caps is None or worker.use_graph(
                lambda pipeline: source_caps.validate_negotiated_caps()
            )
            sink = self._app_sink
            if sink is None:
                raise GStreamerElementError("microphone AppSink is unavailable")
            sink_ready = worker.use_graph(lambda pipeline: sink.validate_negotiated_caps())
            if source_ready and sink_ready:
                return self._calibrate(worker, generation=generation)
            if time.monotonic() >= deadline:
                raise GStreamerElementError("restarted microphone caps negotiation timed out")
            worker.wait_cancelled(0.01)
        raise PipelineError("microphone restart was cancelled")

    def _calibrate(self, worker: _WorkerContext, *, generation: int) -> CaptureContext:
        return worker.use_graph(
            lambda pipeline: calibrate_capture_context(
                pipeline,
                audio_format=self._audio_format,
                generation=generation,
            )
        )

    def _mark_recording_active(self) -> None:
        if self._recording_path is None:
            return
        active_path = recording_segment_path(self._recording_path, self._building_generation)
        with self._recording_lock:
            for recording in self._recordings.values():
                if recording.path == active_path:
                    recording.mark_active()
                    return

    def _finalize_graph(self, graph: _PipelineGraph, deadline: float) -> None:
        with self._recording_lock:
            recording = self._recordings.pop(graph, None)
        if recording is not None:
            recording.finalize(deadline)
