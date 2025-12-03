from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from lumivox_devicelab._gstreamer.audio import RawAudioSpec
from lumivox_devicelab._gstreamer.runtime import GStreamerUnavailableError, get_gst
from lumivox_devicelab._gstreamer.elements.app import AppSrc
from lumivox_devicelab._gstreamer.elements.audio import AudioConvert, AudioResample
from lumivox_devicelab._gstreamer.elements.pipewire import PipeWireSink


def _require_gstreamer() -> None:
    try:
        get_gst()
    except GStreamerUnavailableError as error:
        pytest.skip(str(error))


def test_audio_convert_uses_fixed_quantization_policy() -> None:
    _require_gstreamer()

    element = AudioConvert(RawAudioSpec.mono(rate=16_000))

    assert element.impl.get_property("dithering").value_nick == "none"
    assert element.impl.get_property("noise-shaping").value_nick == "none"


def test_audio_resample_uses_kaiser_at_default_quality() -> None:
    _require_gstreamer()

    element = AudioResample()

    assert element.impl.get_property("quality") == 4
    assert element.impl.get_property("resample-method").value_nick == "kaiser"


def test_audio_convert_accepts_valid_channel_mapping() -> None:
    _require_gstreamer()

    AudioConvert(RawAudioSpec.mapped(rate=16_000, source_channels=4, channel_map={0: 1}))


def test_pipewire_sink_synchronizes_playback_with_pipeline_clock() -> None:
    _require_gstreamer()

    element = PipeWireSink()

    assert element.impl.get_property("sync") is True


@pytest.mark.parametrize(
    ("spec", "expected_max_bytes"),
    [
        (RawAudioSpec(rate=16_000, channels=1), 8_000),
        (RawAudioSpec(rate=48_000, channels=2), 48_000),
    ],
)
def test_appsrc_uses_byte_limit_derived_from_queue_duration(spec: RawAudioSpec, expected_max_bytes: int) -> None:
    _require_gstreamer()

    element = AppSrc(spec)

    assert element.impl.get_property("block") is True
    assert element.impl.get_property("max-bytes") == expected_max_bytes
    assert element.impl.get_property("max-time") == 0
    assert element.impl.get_property("max-buffers") == 0


@pytest.mark.parametrize(
    ("spec", "frames", "expected_sizes", "expected_durations"),
    [
        (RawAudioSpec(rate=16_000, channels=1), 800, [640, 640, 320], [20_000_000, 20_000_000, 10_000_000]),
        (
            RawAudioSpec(rate=48_000, channels=2),
            2_400,
            [3_840, 3_840, 1_920],
            [20_000_000, 20_000_000, 10_000_000],
        ),
    ],
)
def test_appsrc_splits_large_arrays_into_timestamped_buffers(
    spec: RawAudioSpec,
    frames: int,
    expected_sizes: list[int],
    expected_durations: list[int],
) -> None:
    _require_gstreamer()
    element = AppSrc(spec)
    gst = get_gst()
    buffers: list[Any] = []

    class RecordingAppSrc:
        def emit(self, signal_name: str, buffer: Any) -> Any:
            assert signal_name == "push-buffer"
            buffers.append(buffer)
            return gst.FlowReturn.OK

    element._impl = RecordingAppSrc()
    data: np.ndarray[Any, Any] = np.arange(frames * spec.channels, dtype=np.int16)
    if spec.channels > 1:
        data = data.reshape(frames, spec.channels)

    assert element.push(data) == gst.FlowReturn.OK
    assert [buffer.get_size() for buffer in buffers] == expected_sizes
    assert [buffer.pts for buffer in buffers] == [0, 20_000_000, 40_000_000]
    assert [buffer.duration for buffer in buffers] == expected_durations


def test_appsrc_push_eos_returns_gstreamer_flow_result() -> None:
    _require_gstreamer()
    element = AppSrc(RawAudioSpec(rate=16_000, channels=1))
    gst = get_gst()

    class RecordingAppSrc:
        def emit(self, signal_name: str) -> Any:
            assert signal_name == "end-of-stream"
            return gst.FlowReturn.OK

    element._impl = RecordingAppSrc()

    assert element.push_eos() == gst.FlowReturn.OK
