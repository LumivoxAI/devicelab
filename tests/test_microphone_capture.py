from __future__ import annotations

from threading import Event
from unittest.mock import Mock

import numpy as np
import pytest

import lumivox_devicelab
from lumivox_devicelab.state import PipelineState
from lumivox_devicelab.errors import PipelineError, DeviceNotFoundError
from lumivox_devicelab.capture import CapturedChunk, CaptureContext, CaptureHandler
from lumivox_devicelab.formats import AudioFormat, ChannelSelection
from lumivox_devicelab.microphone import MicrophoneCapturePipeline
from lumivox_devicelab._gstreamer.graph import _PipelineGraph
from lumivox_devicelab._gstreamer.elements.app import AppSinkPolicy
from lumivox_devicelab._gstreamer.elements.base import BaseElement
from lumivox_devicelab._gstreamer.elements.flow import QueueOverflowPolicy


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


def test_constructor_is_pure_and_pipeline_is_not_exported(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert "MicrophoneCapturePipeline" not in lumivox_devicelab.__all__
    assert not hasattr(lumivox_devicelab, "MicrophoneCapturePipeline")


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
