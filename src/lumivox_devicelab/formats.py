"""Public audio-format configuration values."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable

from lumivox_devicelab._validation import validate_gst_positive_int
from lumivox_devicelab._gstreamer.audio import RawAudioSpec


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """The normalized sample rate and output channel count."""

    sample_rate: int
    channels: int

    def __post_init__(self) -> None:
        validate_gst_positive_int(self.sample_rate, name="sample_rate")
        validate_gst_positive_int(self.channels, name="channels")


@dataclass(frozen=True, slots=True)
class ChannelSelection:
    """An explicit output-to-source channel mapping for capture."""

    source_channels: int
    mapping: tuple[int, ...]

    def __post_init__(self) -> None:
        source_channels = validate_gst_positive_int(self.source_channels, name="source_channels")
        if not isinstance(self.mapping, Iterable):
            raise TypeError("mapping must be an iterable of integers")
        mapping = tuple(self.mapping)
        if not mapping:
            raise ValueError("mapping must not be empty")
        for source_channel in mapping:
            if isinstance(source_channel, bool) or not isinstance(source_channel, int):
                raise TypeError("mapping entries must be integers")
            if not 0 <= source_channel < source_channels:
                raise ValueError("mapping contains a source channel outside source_channels")
        object.__setattr__(self, "mapping", mapping)


def build_raw_audio_spec(
    audio_format: AudioFormat,
    channel_selection: ChannelSelection | None = None,
) -> RawAudioSpec:
    """Derive the internal raw-audio specification from public configuration."""
    if channel_selection is None:
        return RawAudioSpec(rate=audio_format.sample_rate, channels=audio_format.channels)
    if len(channel_selection.mapping) != audio_format.channels:
        raise ValueError("mapping must contain one source channel per output channel")
    return RawAudioSpec(
        rate=audio_format.sample_rate,
        channels=audio_format.channels,
        source_channels=channel_selection.source_channels,
        channel_map=channel_selection.mapping,
    )
