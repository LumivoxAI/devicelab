"""WAV and FLAC capture pipeline.

This module remains outside the package exports until the complete v1 pipeline
API is released.
"""

from __future__ import annotations

import os
from os import PathLike
from pathlib import Path
from threading import Event

from lumivox_core.logger import Logger

from lumivox_devicelab.state import PipelineState
from lumivox_devicelab.errors import PipelineError
from lumivox_devicelab.capture import CaptureHandler
from lumivox_devicelab.formats import AudioFormat, build_raw_audio_spec

from ._gstreamer.graph import _PipelineGraph
from ._gstreamer.runtime import GStreamerElementError
from ._gstreamer.elements.app import AppSink, AppSinkPolicy, CapturePacket, CapturePacketError
from ._gstreamer.elements.base import BaseElement
from ._gstreamer.elements.file import FileSrc, FlacDec, WavParse, FlacParse, FileReplayMode
from ._gstreamer.elements.flow import ClockSync, AudioQueue, QueueOverflowPolicy
from ._gstreamer.elements.audio import CapsFilter, AudioConvert, AudioResample
from ._gstreamer.capture_delivery import _CaptureDelivery, calibrate_capture_context
from ._gstreamer.pipeline_runtime import _WorkerContext, _PipelineRuntime

_CAPTURE_QUEUE_TIME_MS = 200


class FileCapturePipeline:
    """Deliver normalized PCM from one WAV or FLAC file."""

    def __init__(
        self,
        *,
        logger: Logger,
        handler: CaptureHandler,
        audio_format: AudioFormat,
        path: str | PathLike[str],
        replay_mode: FileReplayMode,
    ) -> None:
        if not isinstance(handler, CaptureHandler):
            raise TypeError("handler must be a CaptureHandler")
        if not isinstance(audio_format, AudioFormat):
            raise TypeError("audio_format must be an AudioFormat")
        if not isinstance(replay_mode, FileReplayMode):
            raise TypeError("replay_mode must be a FileReplayMode")
        path_value = os.fspath(path)
        if not isinstance(path_value, str):
            raise TypeError("path must resolve to a string")
        if not path_value:
            raise ValueError("path must not be empty")
        normalized_path = Path(path_value)
        suffix = normalized_path.suffix.lower()
        if suffix not in (".wav", ".flac"):
            raise ValueError("file capture path must have a .wav or .flac extension")

        self._logger = logger.bind(module="devicelab")
        self._audio_format = audio_format
        self._path = normalized_path
        self._suffix = suffix
        self._replay_mode = replay_mode
        self._spec = build_raw_audio_spec(audio_format)
        self._app_sink: AppSink | None = None
        self._eos = Event()

        self._delivery = _CaptureDelivery(
            logger=self._logger,
            handler=handler,
            pull_packet=self._pull_packet,
            health_observer=self._reject_packet_error,
        )
        self._runtime = _PipelineRuntime(
            logger=self._logger,
            graph_factory=self._create_graph,
            build_graph=self._build_graph,
            readiness=self._ready,
            on_eos=self._note_eos,
        )
        self._runtime.add_worker("capture-delivery", self._delivery.run)
        self._runtime.add_worker("file-eos", self._complete_eos)

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
        return _PipelineGraph(logger=self._logger, name="file-capture")

    def _build_graph(self, graph: _PipelineGraph) -> None:
        source = FileSrc(self._path, name="source")
        elements: list[BaseElement] = [source]
        if self._suffix == ".wav":
            elements.append(WavParse(name="wav-parser"))
        else:
            elements.extend((FlacParse(name="flac-parser"), FlacDec(name="flac-decoder")))
        elements.extend(
            (
                AudioConvert(self._spec, name="convert"),
                AudioResample(name="resample"),
                CapsFilter(self._spec, name="normalized-caps"),
            )
        )
        if self._replay_mode is FileReplayMode.REALTIME:
            elements.append(ClockSync(name="clock-sync"))
            queue_policy = QueueOverflowPolicy.DROP_OLD
            sink_policy = AppSinkPolicy.LIVE_DROP
        else:
            queue_policy = QueueOverflowPolicy.BLOCK
            sink_policy = AppSinkPolicy.BATCH_BLOCK
        elements.append(
            AudioQueue(
                max_time_ms=_CAPTURE_QUEUE_TIME_MS,
                overflow_policy=queue_policy,
                name="capture-queue",
            )
        )
        app_sink = AppSink(self._spec, name="capture-sink", policy=sink_policy)
        app_sink.observe_caps(self._caps_changed)
        elements.append(app_sink)
        graph.add(*elements)
        graph.link(*elements)
        self._app_sink = app_sink

    def _ready(self, worker: _WorkerContext) -> None:
        while not worker.cancelled:
            sink = self._app_sink
            if sink is None:
                raise GStreamerElementError("file capture AppSink is unavailable")
            if worker.use_graph(lambda pipeline: sink.validate_negotiated_caps()):
                context = worker.use_graph(
                    lambda pipeline: calibrate_capture_context(
                        pipeline,
                        audio_format=self._audio_format,
                        generation=0,
                    )
                )
                self._delivery.activate(worker, context)
                return
            worker.wait_cancelled(0.01)

    def _pull_packet(self, timeout_ms: int) -> CapturePacket | None:
        sink = self._app_sink
        if sink is None:
            raise GStreamerElementError("file capture AppSink is unavailable")
        return sink.try_pull(timeout_ms)

    @staticmethod
    def _reject_packet_error(error: CapturePacketError) -> None:
        raise error

    def _caps_changed(self, error: CapturePacketError) -> None:
        self._runtime.report_failure("file capture caps are incompatible", error)

    def _note_eos(self) -> None:
        self._eos.set()

    def _complete_eos(self, worker: _WorkerContext) -> None:
        while not self._eos.wait(0.01):
            if worker.cancelled:
                return
        while self.state is PipelineState.STARTING:
            if worker.wait_cancelled(0.01):
                return
        if self.state is PipelineState.RUNNING and not worker.cancelled:
            self._delivery.notify_eos()
