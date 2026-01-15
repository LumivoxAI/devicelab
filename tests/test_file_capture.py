from __future__ import annotations

import time
import wave
from typing import Any, cast
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import numpy as np
import pytest

import lumivox_devicelab
from lumivox_devicelab.state import PipelineState
from lumivox_devicelab.errors import PipelineError
from lumivox_devicelab.capture import CapturedChunk, CaptureContext, CaptureHandler
from lumivox_devicelab.formats import AudioFormat
from lumivox_devicelab.file_capture import FileCapturePipeline
from lumivox_devicelab._gstreamer.graph import _PipelineGraph
from lumivox_devicelab._gstreamer.runtime import get_gst
from lumivox_devicelab._gstreamer.elements.app import (
    AppSinkPolicy,
    CapturePacketError,
    CapturePacketErrorKind,
)
from lumivox_devicelab._gstreamer.elements.file import FileReplayMode
from lumivox_devicelab._gstreamer.elements.flow import QueueOverflowPolicy


class RecordingHandler(CaptureHandler):
    def __init__(self) -> None:
        self.events: list[str] = []
        self.chunks: list[CapturedChunk] = []
        self.started = Event()
        self.stopped = Event()
        self.stop_cause: PipelineError | None = None

    def on_start(self, context: CaptureContext) -> None:
        self.events.append("start")
        self.started.set()

    def on_chunk(self, chunk: CapturedChunk) -> None:
        self.events.append("chunk")
        self.chunks.append(chunk)

    def on_stop(self, context: CaptureContext, cause: PipelineError | None) -> None:
        self.events.append("stop")
        self.stop_cause = cause
        self.stopped.set()


def _logger() -> Mock:
    logger = Mock()
    logger.bind.return_value = logger
    return logger


def _pipeline(path: Path, **kwargs: Any) -> FileCapturePipeline:
    return FileCapturePipeline(
        logger=_logger(),
        handler=RecordingHandler(),
        audio_format=AudioFormat(8_000, 1),
        path=path,
        replay_mode=FileReplayMode.AS_FAST_AS_POSSIBLE,
        **kwargs,
    )


def _write_wav(path: Path, samples: np.ndarray, *, sample_rate: int = 8_000) -> None:
    channels = 1 if samples.ndim == 1 else samples.shape[1]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples.astype("<i2", copy=False).tobytes())


def _transcode_wav_to_flac(source_path: Path, destination_path: Path) -> None:
    gst = get_gst()
    pipeline = gst.Pipeline.new("test-flac-writer")
    elements = [
        gst.ElementFactory.make("filesrc"),
        gst.ElementFactory.make("wavparse"),
        gst.ElementFactory.make("audioconvert"),
        gst.ElementFactory.make("flacenc"),
        gst.ElementFactory.make("filesink"),
    ]
    assert pipeline is not None and all(element is not None for element in elements)
    elements[0].set_property("location", str(source_path))
    elements[-1].set_property("location", str(destination_path))
    for element in elements:
        pipeline.add(element)
    for first, second in zip(elements, elements[1:]):
        assert first.link(second)
    try:
        assert pipeline.set_state(gst.State.PLAYING) != gst.StateChangeReturn.FAILURE
        message = pipeline.get_bus().timed_pop_filtered(3 * gst.SECOND, gst.MessageType.ERROR | gst.MessageType.EOS)
        assert message is not None
        if message.type == gst.MessageType.ERROR:
            error, debug = message.parse_error()
            pytest.fail(f"FLAC fixture encoding failed: {error}; {debug}")
    finally:
        pipeline.set_state(gst.State.NULL)


def _capture_file(path: Path, audio_format: AudioFormat, replay_mode: FileReplayMode) -> RecordingHandler:
    handler = RecordingHandler()
    pipeline = FileCapturePipeline(
        logger=_logger(),
        handler=handler,
        audio_format=audio_format,
        path=path,
        replay_mode=replay_mode,
    )
    pipeline.start()
    pipeline.wait(timeout=3)
    return handler


def _frame_running_times(handler: RecordingHandler, sample_rate: int) -> np.ndarray:
    return np.concatenate(
        [
            chunk.running_time_ns + np.arange(chunk.samples.shape[0], dtype=np.int64) * 1_000_000_000 // sample_rate
            for chunk in handler.chunks
        ]
    )


def test_constructor_is_pure_and_pipeline_is_not_exported(tmp_path: Path) -> None:
    missing = tmp_path / "missing.WAV"
    pipeline = _pipeline(missing)

    assert pipeline.state is PipelineState.CREATED
    assert pipeline.failure is None
    assert "FileCapturePipeline" not in lumivox_devicelab.__all__
    assert not hasattr(lumivox_devicelab, "FileCapturePipeline")


@pytest.mark.parametrize("suffix", [".wav", ".WAV", ".flac", ".FLAC"])
def test_supported_extensions_are_case_insensitive(tmp_path: Path, suffix: str) -> None:
    _pipeline(tmp_path / f"input{suffix}")


@pytest.mark.parametrize("name", ["input", "input.mp3", "input.wav.tmp"])
def test_unsupported_extensions_are_configuration_errors(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match=".wav or .flac"):
        _pipeline(tmp_path / name)


def test_replay_mode_requires_the_enum(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="FileReplayMode"):
        FileCapturePipeline(
            logger=_logger(),
            handler=RecordingHandler(),
            audio_format=AudioFormat(8_000, 1),
            path=tmp_path / "input.wav",
            replay_mode=cast(Any, "as_fast_as_possible"),
        )


@pytest.mark.parametrize(
    ("suffix", "mode", "expected_kinds", "queue_policy", "sink_policy"),
    [
        (
            ".wav",
            FileReplayMode.REALTIME,
            [
                "FileSrc",
                "WavParse",
                "AudioConvert",
                "AudioResample",
                "CapsFilter",
                "ClockSync",
                "AudioQueue",
                "AppSink",
            ],
            QueueOverflowPolicy.DROP_OLD,
            AppSinkPolicy.LIVE_DROP,
        ),
        (
            ".flac",
            FileReplayMode.AS_FAST_AS_POSSIBLE,
            ["FileSrc", "FlacParse", "FlacDec", "AudioConvert", "AudioResample", "CapsFilter", "AudioQueue", "AppSink"],
            QueueOverflowPolicy.BLOCK,
            AppSinkPolicy.BATCH_BLOCK,
        ),
    ],
)
def test_graph_uses_format_and_replay_specific_bounded_chain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    suffix: str,
    mode: FileReplayMode,
    expected_kinds: list[str],
    queue_policy: QueueOverflowPolicy,
    sink_policy: AppSinkPolicy,
) -> None:
    created: list[tuple[str, dict[str, object]]] = []

    class FakeElement:
        def __init__(self, kind: str, kwargs: dict[str, object]) -> None:
            self.kind = kind
            created.append((kind, kwargs))

        def observe_caps(self, callback: object) -> None:
            del callback

    def factory(kind: str):  # type: ignore[no-untyped-def]
        return lambda *args, **kwargs: FakeElement(kind, kwargs)

    for name in (
        "FileSrc",
        "WavParse",
        "FlacParse",
        "FlacDec",
        "AudioConvert",
        "AudioResample",
        "CapsFilter",
        "ClockSync",
        "AudioQueue",
        "AppSink",
    ):
        monkeypatch.setattr(f"lumivox_devicelab.file_capture.{name}", factory(name))

    graph = Mock(spec=_PipelineGraph)
    pipeline = FileCapturePipeline(
        logger=_logger(),
        handler=RecordingHandler(),
        audio_format=AudioFormat(16_000, 1),
        path=tmp_path / f"input{suffix}",
        replay_mode=mode,
    )
    pipeline._build_graph(graph)

    assert [kind for kind, _ in created] == expected_kinds
    assert [element.kind for element in graph.link.call_args.args] == expected_kinds
    queue = next(kwargs for kind, kwargs in created if kind == "AudioQueue")
    assert queue["max_time_ms"] == 200
    assert queue["overflow_policy"] is queue_policy
    sink = next(kwargs for kind, kwargs in created if kind == "AppSink")
    assert sink["policy"] is sink_policy


@pytest.mark.parametrize("kind", list(CapturePacketErrorKind))
def test_every_packet_error_is_terminal(kind: CapturePacketErrorKind, tmp_path: Path) -> None:
    error = CapturePacketError(kind, "invalid file packet")

    with pytest.raises(CapturePacketError) as raised:
        _pipeline(tmp_path / "input.wav")._reject_packet_error(error)

    assert raised.value is error


def test_incompatible_caps_are_reported_as_a_retained_failure(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path / "input.wav")
    error = CapturePacketError(CapturePacketErrorKind.CAPS, "changed caps")
    pipeline._caps_changed(error)

    assert pipeline.failure is not None
    assert pipeline.failure.__cause__ is error


@pytest.mark.parametrize("suffix", [".wav", ".flac"])
@pytest.mark.gstreamer(
    factories=(
        "filesrc",
        "wavparse",
        "flacparse",
        "flacdec",
        "flacenc",
        "audioconvert",
        "audioresample",
        "capsfilter",
        "queue",
        "appsink",
    )
)
def test_fast_replay_delivers_every_sample_before_normal_stop(tmp_path: Path, suffix: str) -> None:
    samples = np.arange(-400, 400, dtype="<i2")
    wav_path = tmp_path / "samples.wav"
    _write_wav(wav_path, samples)
    path = tmp_path / f"samples{suffix}"
    if suffix == ".flac":
        _transcode_wav_to_flac(wav_path, path)
    handler = RecordingHandler()
    pipeline = FileCapturePipeline(
        logger=_logger(),
        handler=handler,
        audio_format=AudioFormat(8_000, 1),
        path=path,
        replay_mode=FileReplayMode.AS_FAST_AS_POSSIBLE,
    )

    pipeline.start()
    pipeline.wait(timeout=3)

    delivered = np.concatenate([chunk.samples for chunk in handler.chunks])
    np.testing.assert_array_equal(delivered, samples)
    assert handler.events[0] == "start"
    assert handler.events[-1] == "stop"
    assert handler.stop_cause is None
    assert pipeline.state is PipelineState.STOPPED
    assert pipeline.failure is None


@pytest.mark.gstreamer(
    factories=(
        "filesrc",
        "wavparse",
        "audioconvert",
        "audioresample",
        "capsfilter",
        "clocksync",
        "queue",
        "appsink",
    )
)
def test_replay_modes_preserve_samples_and_relative_media_timeline(tmp_path: Path) -> None:
    samples = np.arange(-400, 400, dtype="<i2")
    path = tmp_path / "timeline.wav"
    _write_wav(path, samples)

    fast = _capture_file(path, AudioFormat(8_000, 1), FileReplayMode.AS_FAST_AS_POSSIBLE)
    realtime = _capture_file(path, AudioFormat(8_000, 1), FileReplayMode.REALTIME)

    np.testing.assert_array_equal(
        np.concatenate([chunk.samples for chunk in fast.chunks]),
        np.concatenate([chunk.samples for chunk in realtime.chunks]),
    )
    np.testing.assert_array_equal(_frame_running_times(fast, 8_000), _frame_running_times(realtime, 8_000))
    for handler in (fast, realtime):
        running_times = _frame_running_times(handler, 8_000)
        relative_capture_times = np.concatenate(
            [
                chunk.captured_at_ns
                - handler.chunks[0].captured_at_ns
                + np.arange(chunk.samples.shape[0], dtype=np.int64) * 1_000_000_000 // 8_000
                for chunk in handler.chunks
            ]
        )
        np.testing.assert_array_equal(relative_capture_times, running_times - running_times[0])


@pytest.mark.gstreamer(
    factories=("filesrc", "wavparse", "audioconvert", "audioresample", "capsfilter", "queue", "appsink")
)
def test_fast_replay_converts_channels_and_expected_resampled_frame_count(tmp_path: Path) -> None:
    frames = 400
    stereo = np.column_stack(
        (
            np.arange(frames, dtype="<i2"),
            np.arange(frames, dtype="<i2"),
        )
    )
    path = tmp_path / "stereo.wav"
    _write_wav(path, stereo, sample_rate=8_000)

    handler = _capture_file(path, AudioFormat(16_000, 1), FileReplayMode.AS_FAST_AS_POSSIBLE)
    delivered = np.concatenate([chunk.samples for chunk in handler.chunks])

    assert delivered.dtype == np.dtype("<i2")
    assert delivered.ndim == 1
    assert delivered.shape == (frames * 2,)


@pytest.mark.gstreamer(
    factories=(
        "filesrc",
        "wavparse",
        "audioconvert",
        "audioresample",
        "capsfilter",
        "clocksync",
        "queue",
        "appsink",
    )
)
def test_realtime_replay_can_be_stopped_before_eos(tmp_path: Path) -> None:
    path = tmp_path / "long.wav"
    _write_wav(path, np.arange(16_000, dtype="<i2"))
    handler = RecordingHandler()
    pipeline = FileCapturePipeline(
        logger=_logger(),
        handler=handler,
        audio_format=AudioFormat(8_000, 1),
        path=path,
        replay_mode=FileReplayMode.REALTIME,
    )

    pipeline.start()
    pipeline.stop(timeout=3)

    assert handler.events[0] == "start"
    assert handler.events[-1] == "stop"
    assert handler.stop_cause is None
    assert pipeline.state is PipelineState.STOPPED


@pytest.mark.gstreamer(
    factories=(
        "filesrc",
        "wavparse",
        "audioconvert",
        "audioresample",
        "capsfilter",
        "clocksync",
        "queue",
        "appsink",
    )
)
def test_realtime_slow_handler_drops_old_audio_and_marks_the_gap(tmp_path: Path) -> None:
    class SlowHandler(RecordingHandler):
        def on_chunk(self, chunk: CapturedChunk) -> None:
            super().on_chunk(chunk)
            time.sleep(0.35)

    samples = np.arange(24_000, dtype="<i2")
    path = tmp_path / "live.wav"
    _write_wav(path, samples)
    handler = SlowHandler()
    pipeline = FileCapturePipeline(
        logger=_logger(),
        handler=handler,
        audio_format=AudioFormat(8_000, 1),
        path=path,
        replay_mode=FileReplayMode.REALTIME,
    )

    pipeline.start()
    pipeline.wait(timeout=6)

    delivered_frames = sum(chunk.samples.shape[0] for chunk in handler.chunks)
    assert delivered_frames < samples.shape[0]
    assert any(chunk.discontinuity for chunk in handler.chunks[1:])


@pytest.mark.gstreamer(
    factories=("filesrc", "wavparse", "audioconvert", "audioresample", "capsfilter", "queue", "appsink")
)
def test_handler_chunk_error_is_retained(tmp_path: Path) -> None:
    callback_error = RuntimeError("handler failed")

    class FailingHandler(RecordingHandler):
        def on_chunk(self, chunk: CapturedChunk) -> None:
            del chunk
            raise callback_error

    path = tmp_path / "samples.wav"
    _write_wav(path, np.arange(800, dtype="<i2"))
    pipeline = FileCapturePipeline(
        logger=_logger(),
        handler=FailingHandler(),
        audio_format=AudioFormat(8_000, 1),
        path=path,
        replay_mode=FileReplayMode.AS_FAST_AS_POSSIBLE,
    )

    try:
        pipeline.start(timeout=3)
    except PipelineError:
        pass
    with pytest.raises(PipelineError) as raised:
        pipeline.wait(timeout=3)

    assert raised.value is pipeline.failure
    assert raised.value.__cause__ is callback_error


@pytest.mark.gstreamer(
    factories=("filesrc", "wavparse", "audioconvert", "audioresample", "capsfilter", "queue", "appsink")
)
def test_empty_valid_wav_has_complete_handler_lifecycle_without_chunks(tmp_path: Path) -> None:
    path = tmp_path / "empty.wav"
    _write_wav(path, np.array([], dtype="<i2"))
    handler = RecordingHandler()
    pipeline = FileCapturePipeline(
        logger=_logger(),
        handler=handler,
        audio_format=AudioFormat(8_000, 1),
        path=path,
        replay_mode=FileReplayMode.AS_FAST_AS_POSSIBLE,
    )

    pipeline.start()
    pipeline.wait(timeout=3)

    assert handler.events == ["start", "stop"]
    assert not handler.chunks
    assert handler.stop_cause is None


@pytest.mark.gstreamer(
    factories=("filesrc", "wavparse", "audioconvert", "audioresample", "capsfilter", "queue", "appsink")
)
def test_malformed_file_is_a_retained_pipeline_failure(tmp_path: Path) -> None:
    path = tmp_path / "broken.wav"
    path.write_bytes(b"not a wav file")
    pipeline = _pipeline(path)

    with pytest.raises(PipelineError) as raised:
        pipeline.start(timeout=3)

    assert raised.value is pipeline.failure
    assert raised.value.__cause__ is not None
    assert pipeline.state is PipelineState.STOPPED
