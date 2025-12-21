from __future__ import annotations

from typing import Any
from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from lumivox_devicelab import (
    AudioFormat,
    DeviceError,
    CapturedChunk,
    PipelineError,
    PipelineState,
    CaptureContext,
    CaptureHandler,
    DevicelabError,
    ChannelSelection,
    PipelineStateError,
    DeviceNotFoundError,
    PipelineTimeoutError,
    PlaybackSubmissionError,
)
from lumivox_devicelab.formats import build_raw_audio_spec
from lumivox_devicelab._validation import GST_INT_MAX, validate_timeout


@pytest.mark.parametrize("field", ["sample_rate", "channels"])
@pytest.mark.parametrize("value", [True, 1.0, 0, -1, GST_INT_MAX + 1])
def test_audio_format_rejects_invalid_values(field: str, value: object) -> None:
    values: dict[str, object] = {"sample_rate": 16_000, "channels": 1, field: value}
    with pytest.raises((TypeError, ValueError), match=field):
        AudioFormat(**values)  # type: ignore[arg-type]


def test_audio_format_is_frozen() -> None:
    audio_format = AudioFormat(sample_rate=16_000, channels=1)
    with pytest.raises(FrozenInstanceError):
        audio_format.channels = 2  # type: ignore[misc]


def test_channel_selection_copies_mapping_and_allows_repetition() -> None:
    caller_mapping = [1, 1]
    selection = ChannelSelection(source_channels=2, mapping=caller_mapping)  # type: ignore[arg-type]
    caller_mapping[0] = 0
    assert selection.mapping == (1, 1)


@pytest.mark.parametrize(
    ("source_channels", "mapping", "message"),
    [
        (0, (0,), "source_channels"),
        (2, (), "empty"),
        (2, (2,), "outside"),
        (2, (-1,), "outside"),
        (2, (False,), "integers"),
    ],
)
def test_channel_selection_rejects_invalid_values(source_channels: int, mapping: tuple[Any, ...], message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        ChannelSelection(source_channels=source_channels, mapping=mapping)


def test_raw_audio_spec_derivation_requires_one_mapping_per_output_channel() -> None:
    audio_format = AudioFormat(sample_rate=16_000, channels=2)
    with pytest.raises(ValueError, match="one source channel per output channel"):
        build_raw_audio_spec(audio_format, ChannelSelection(source_channels=2, mapping=(0,)))


def test_raw_audio_spec_derivation_preserves_public_configuration() -> None:
    audio_format = AudioFormat(sample_rate=48_000, channels=2)
    selection = ChannelSelection(source_channels=4, mapping=(2, 0))
    spec = build_raw_audio_spec(audio_format, selection)
    assert (spec.rate, spec.channels, spec.source_channels, spec.channel_map) == (48_000, 2, 4, (2, 0))


def test_pipeline_state_has_exact_values() -> None:
    assert [state.value for state in PipelineState] == ["created", "starting", "running", "stopping", "stopped"]


def test_error_hierarchy_and_read_only_secondary_view() -> None:
    cause = RuntimeError("primary")
    error = PipelineError("pipeline failed", cause=cause)
    secondary = RuntimeError("cleanup")
    error._add_secondary_error(secondary)
    view = error.secondary_errors
    error._add_secondary_error(RuntimeError("later"))

    assert isinstance(DeviceNotFoundError("missing"), DeviceError)
    assert isinstance(DeviceError("device"), DevicelabError)
    assert isinstance(PipelineStateError("state"), PipelineError)
    assert isinstance(PipelineTimeoutError("timeout"), TimeoutError)
    assert error.__cause__ is cause
    assert view == (secondary,)
    assert error.secondary_errors[0] is secondary


@pytest.mark.parametrize("accepted_frames", [True, 1.0, -1])
def test_playback_submission_error_rejects_invalid_accepted_frames(accepted_frames: object) -> None:
    with pytest.raises((TypeError, ValueError), match="accepted_frames"):
        PlaybackSubmissionError("interrupted", accepted_frames=accepted_frames)  # type: ignore[arg-type]


def test_playback_submission_error_exposes_accepted_frames() -> None:
    assert PlaybackSubmissionError("interrupted", accepted_frames=12).accepted_frames == 12


def test_capture_context_is_frozen_and_validates_non_negative_fields() -> None:
    context = CaptureContext(AudioFormat(16_000, 1), 0, 1, 0)
    with pytest.raises(FrozenInstanceError):
        context.generation = 1  # type: ignore[misc]
    with pytest.raises(ValueError, match="generation"):
        CaptureContext(AudioFormat(16_000, 1), -1, 1, 0)


def test_captured_chunk_copies_non_empty_s16le_samples() -> None:
    source = np.array([[1, 2], [3, 4]], dtype="<i2")
    chunk = CapturedChunk(source, generation=0, captured_at_ns=1, running_time_ns=0, discontinuity=False)
    source[0, 0] = 99
    chunk.samples[0, 1] = 88
    assert chunk.samples[0].tolist() == [1, 88]


@pytest.mark.parametrize(
    "samples",
    [
        np.array([], dtype="<i2"),
        np.empty((1, 0), dtype="<i2"),
        np.empty((1, 1, 1), dtype="<i2"),
        np.array([1], dtype=">i2"),
        np.array([1], dtype=np.float32),
    ],
)
def test_captured_chunk_rejects_invalid_samples(samples: np.ndarray[Any, Any]) -> None:
    with pytest.raises(ValueError):
        CapturedChunk(samples, generation=0, captured_at_ns=0, running_time_ns=0, discontinuity=False)


class ConcreteHandler(CaptureHandler):
    def on_chunk(self, chunk: CapturedChunk) -> None:
        pass


def test_capture_handler_only_requires_on_chunk() -> None:
    handler = ConcreteHandler()
    context = CaptureContext(AudioFormat(16_000, 1), 0, 0, 0)
    handler.on_start(context)
    handler.on_restart(context, PipelineError("restart"))
    handler.on_stop(context, None)
    with pytest.raises(TypeError):
        CaptureHandler()  # type: ignore[abstract]


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("-inf"), float("nan")])
def test_timeout_validator_rejects_non_positive_or_non_finite_values(timeout: float) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        validate_timeout(timeout)


@pytest.mark.parametrize("timeout", [None, True, "1"])
def test_timeout_validator_rejects_non_numeric_values(timeout: object) -> None:
    with pytest.raises(TypeError, match="positive finite"):
        validate_timeout(timeout)


def test_timeout_validator_normalizes_numbers_and_optionally_accepts_none() -> None:
    assert validate_timeout(2) == 2.0
    assert validate_timeout(None, allow_none=True) is None
