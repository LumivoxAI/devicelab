"""Public values and handler contract for captured audio."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from lumivox_devicelab.errors import PipelineError
from lumivox_devicelab.formats import AudioFormat
from lumivox_devicelab._validation import validate_non_negative_int
from lumivox_devicelab._gstreamer.audio import S16LE_DTYPE


@dataclass(frozen=True, slots=True)
class CaptureContext:
    """Timestamp calibration and format for one capture generation."""

    audio_format: AudioFormat
    generation: int
    wall_time_anchor_ns: int
    running_time_anchor_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.audio_format, AudioFormat):
            raise TypeError("audio_format must be an AudioFormat")
        validate_non_negative_int(self.generation, name="generation")
        validate_non_negative_int(self.wall_time_anchor_ns, name="wall_time_anchor_ns")
        validate_non_negative_int(self.running_time_anchor_ns, name="running_time_anchor_ns")


@dataclass(frozen=True, slots=True)
class CapturedChunk:
    """A receiver-owned block of normalized captured PCM."""

    samples: NDArray[np.int16]
    generation: int
    captured_at_ns: int
    running_time_ns: int
    discontinuity: bool

    def __post_init__(self) -> None:
        if not isinstance(self.samples, np.ndarray):
            raise TypeError("samples must be a numpy.ndarray")
        if self.samples.dtype != S16LE_DTYPE:
            raise ValueError("samples dtype must be little-endian numpy.int16 (PCM S16LE)")
        if self.samples.ndim not in (1, 2):
            raise ValueError("samples must have one or two dimensions")
        if self.samples.shape[0] == 0 or (self.samples.ndim == 2 and self.samples.shape[1] == 0):
            raise ValueError("samples must contain at least one frame and channel")
        validate_non_negative_int(self.generation, name="generation")
        validate_non_negative_int(self.captured_at_ns, name="captured_at_ns")
        validate_non_negative_int(self.running_time_ns, name="running_time_ns")
        if not isinstance(self.discontinuity, bool):
            raise TypeError("discontinuity must be a bool")
        object.__setattr__(self, "samples", self.samples.copy())


class CaptureHandler(ABC):
    """Synchronous callbacks invoked serially by capture delivery."""

    def on_start(self, context: CaptureContext) -> None:
        """Handle successful startup before the first chunk."""

    @abstractmethod
    def on_chunk(self, chunk: CapturedChunk) -> None:
        """Handle a captured audio chunk."""

    def on_restart(self, context: CaptureContext, cause: PipelineError) -> None:
        """Handle successful recovery before capture delivery resumes."""

    def on_stop(self, context: CaptureContext, cause: PipelineError | None) -> None:
        """Handle terminal shutdown after capture delivery ends."""
