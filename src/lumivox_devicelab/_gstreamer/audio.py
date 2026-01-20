"""Pure-Python validation for Devicelab's fixed raw-audio contract."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

import numpy as np

S16LE_DTYPE = np.dtype("<i2")
S16LE_BYTES_PER_SAMPLE = S16LE_DTYPE.itemsize


@dataclass(frozen=True, slots=True)
class RawAudioSpec:
    """The raw audio format and optional source-channel mapping at Devicelab's boundary."""

    rate: int
    channels: int
    source_channels: int | None = None
    channel_map: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError("rate must be positive")
        if self.channels <= 0:
            raise ValueError("channels must be positive")
        if (self.source_channels is None) != (self.channel_map is None):
            raise ValueError("source_channels and channel_map must be provided together")
        if self.source_channels is not None and self.source_channels <= 0:
            raise ValueError("source_channels must be positive")
        if self.channel_map is not None:
            assert self.source_channels is not None
            if len(self.channel_map) != self.channels:
                raise ValueError("channel_map must contain one source channel per output channel")
            if any(not 0 <= source_channel < self.source_channels for source_channel in self.channel_map):
                raise ValueError("channel_map contains a source channel outside source_channels")

    @classmethod
    def mono(cls, rate: int) -> RawAudioSpec:
        """Request mono output with GStreamer's standard channel conversion."""
        return cls(rate=rate, channels=1)

    @classmethod
    def mapped(
        cls,
        rate: int,
        source_channels: int,
        channel_map: Mapping[int, int],
    ) -> RawAudioSpec:
        """Request output channels selected explicitly from a known source layout."""
        if not channel_map:
            raise ValueError("channel_map must not be empty")
        output_channels = len(channel_map)
        if set(channel_map) != set(range(output_channels)):
            raise ValueError("channel_map keys must be consecutive output channel indices starting at 0")
        return cls(
            rate=rate,
            channels=output_channels,
            source_channels=source_channels,
            channel_map=tuple(channel_map[output_channel] for output_channel in range(output_channels)),
        )

    @property
    def frame_size(self) -> int:
        return self.channels * S16LE_BYTES_PER_SAMPLE


def validate_pcm_array(data: np.ndarray, spec: RawAudioSpec) -> int:
    """Validate a public PCM buffer and return its number of frames."""
    if not isinstance(data, np.ndarray):
        raise TypeError("data must be a numpy.ndarray")
    if data.dtype != S16LE_DTYPE:
        raise ValueError("data dtype must be little-endian numpy.int16 (PCM S16LE)")
    if spec.channels == 1:
        if data.ndim != 1:
            raise ValueError("mono data must have shape (frames,)")
    elif data.ndim != 2 or data.shape[1] != spec.channels:
        raise ValueError(f"{spec.channels}-channel data must have shape (frames, {spec.channels})")

    frames = int(data.shape[0])
    if frames == 0:
        raise ValueError("data must contain at least one frame")
    return frames
