from __future__ import annotations

import numpy as np
import pytest

from lumivox_devicelab._gstreamer.audio import RawAudioSpec, validate_pcm_array


def test_raw_audio_spec_requires_positive_rate() -> None:
    with pytest.raises(ValueError, match="rate must be positive"):
        RawAudioSpec(rate=0, channels=1)


def test_raw_audio_spec_requires_positive_channels() -> None:
    with pytest.raises(ValueError, match="channels must be positive"):
        RawAudioSpec(rate=16_000, channels=0)


def test_raw_audio_spec_calculates_frame_size() -> None:
    assert RawAudioSpec(rate=48_000, channels=2).frame_size == 4


def test_raw_audio_spec_mono_uses_standard_channel_conversion() -> None:
    spec = RawAudioSpec.mono(rate=16_000)

    assert spec.channels == 1
    assert spec.source_channels is None
    assert spec.channel_map is None


def test_raw_audio_spec_mapped_selects_source_channels() -> None:
    spec = RawAudioSpec.mapped(rate=16_000, source_channels=4, channel_map={0: 2, 1: 0})

    assert spec.channels == 2
    assert spec.source_channels == 4
    assert spec.channel_map == (2, 0)


@pytest.mark.parametrize(
    ("source_channels", "channel_map", "message"),
    [
        (4, {}, "must not be empty"),
        (4, {1: 0}, "keys"),
        (4, {0: 4}, "outside"),
        (0, {0: 0}, "source_channels"),
    ],
)
def test_raw_audio_spec_mapped_rejects_invalid_channel_mapping(
    source_channels: int,
    channel_map: dict[int, int],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        RawAudioSpec.mapped(rate=16_000, source_channels=source_channels, channel_map=channel_map)


def test_validate_pcm_array_accepts_mono_s16le() -> None:
    data = np.array([1, -2, 3], dtype=np.int16)

    assert validate_pcm_array(data, RawAudioSpec(rate=16_000, channels=1)) == 3


def test_validate_pcm_array_accepts_multichannel_s16le() -> None:
    data = np.array([[1, -2], [3, -4]], dtype=np.int16)

    assert validate_pcm_array(data, RawAudioSpec(rate=48_000, channels=2)) == 2


@pytest.mark.parametrize(
    ("data", "channels", "message"),
    [
        (np.array([1], dtype=np.float32), 1, "dtype"),
        (np.array([1], dtype=">i2"), 1, "dtype"),
        (np.array([[1]], dtype=np.int16), 1, "mono"),
        (np.array([1], dtype=np.int16), 2, "2-channel"),
        (np.array([[1]], dtype=np.int16), 2, "2-channel"),
        (np.array([], dtype=np.int16), 1, "at least one frame"),
    ],
)
def test_validate_pcm_array_rejects_invalid_data(data: np.ndarray, channels: int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_pcm_array(data, RawAudioSpec(rate=16_000, channels=channels))
