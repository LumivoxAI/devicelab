"""PipeWire microphone capture pipeline.

This module remains outside the package exports until the complete v1 pipeline
API is released.
"""

from __future__ import annotations

from lumivox_core.logger import Logger

from lumivox_devicelab.state import PipelineState
from lumivox_devicelab.errors import PipelineError
from lumivox_devicelab.capture import CaptureHandler
from lumivox_devicelab.formats import AudioFormat, ChannelSelection, build_raw_audio_spec

from ._gstreamer.graph import _PipelineGraph
from ._gstreamer.runtime import GStreamerElementError
from ._gstreamer.elements.app import AppSink, AppSinkPolicy, CapturePacket, CapturePacketError
from ._gstreamer.elements.base import BaseElement
from ._gstreamer.elements.flow import AudioQueue, QueueOverflowPolicy
from ._gstreamer.elements.audio import CapsFilter, AudioConvert, AudioResample, SourceChannelCapsFilter
from ._gstreamer.capture_delivery import _CaptureDelivery, calibrate_capture_context
from ._gstreamer.device_discovery import DeviceDirection, resolve_pipewire_target
from ._gstreamer.pipeline_runtime import _WorkerContext, _PipelineRuntime
from ._gstreamer.elements.pipewire import PipeWireSrc

_CAPTURE_QUEUE_TIME_MS = 200


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

        self._logger = logger.bind(module="devicelab")
        self._audio_format = audio_format
        self._device_id = device_id
        self._spec = build_raw_audio_spec(audio_format, channel_selection)
        self._channel_selection = channel_selection
        self._target_object: str | None = None
        self._app_sink: AppSink | None = None
        self._source_caps: SourceChannelCapsFilter | None = None

        self._delivery = _CaptureDelivery(
            logger=self._logger,
            handler=handler,
            pull_packet=self._pull_packet,
            health_observer=self._fail_packet,
        )
        self._runtime = _PipelineRuntime(
            logger=self._logger,
            graph_factory=self._create_graph,
            build_graph=self._build_graph,
            readiness=self._ready,
            on_eos=self._unexpected_eos,
        )
        self._runtime.add_worker("capture-delivery", self._delivery.run)

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
        queue = AudioQueue(
            max_time_ms=_CAPTURE_QUEUE_TIME_MS,
            overflow_policy=QueueOverflowPolicy.DROP_OLD,
            name="capture-queue",
        )
        app_sink = AppSink(self._spec, name="capture-sink", policy=AppSinkPolicy.LIVE_DROP)

        elements: list[BaseElement] = [source]
        source_caps = None
        if self._channel_selection is not None:
            source_caps = SourceChannelCapsFilter(self._channel_selection.source_channels, name="source-channels")
            elements.append(source_caps)
        elements.extend((convert, resample, normalized_caps, queue, app_sink))
        graph.add(*elements)
        graph.link(*elements)
        self._source_caps = source_caps
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
            raise GStreamerElementError("microphone AppSink is unavailable")
        return sink.try_pull(timeout_ms)

    @staticmethod
    def _fail_packet(error: CapturePacketError) -> None:
        raise error

    @staticmethod
    def _unexpected_eos() -> None:
        raise GStreamerElementError("microphone pipeline reached unexpected EOS")
