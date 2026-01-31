from __future__ import annotations

from typing import Any, cast
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from lumivox_devicelab.state import PipelineState
from lumivox_devicelab.errors import PipelineStateError
from lumivox_devicelab.capture import CapturedChunk, CaptureHandler
from lumivox_devicelab.formats import AudioFormat
from lumivox_devicelab.speaker import SpeakerPlaybackPipeline
from lumivox_devicelab.microphone import MicrophoneCapturePipeline
from lumivox_devicelab.file_capture import FileReplayMode, FileCapturePipeline


class _Handler(CaptureHandler):
    def on_chunk(self, chunk: CapturedChunk) -> None:
        del chunk


class _Runtime:
    def __init__(self, state: PipelineState) -> None:
        self.state = state
        self.operations = 0

    def use_graph(self, operation: Any) -> None:
        self.operations += 1
        operation(object())


def _logger() -> Mock:
    logger = Mock()
    logger.bind.return_value = logger
    return logger


def _pipelines(tmp_path: Path, *, volume: float = 1.0) -> tuple[Any, ...]:
    audio_format = AudioFormat(16_000, 1)
    return (
        MicrophoneCapturePipeline(
            logger=_logger(), handler=_Handler(), audio_format=audio_format, device_id="microphone", volume=volume
        ),
        FileCapturePipeline(
            logger=_logger(),
            handler=_Handler(),
            audio_format=audio_format,
            path=tmp_path / "input.wav",
            replay_mode=FileReplayMode.REALTIME,
            volume=volume,
        ),
        SpeakerPlaybackPipeline(logger=_logger(), audio_format=audio_format, device_id="speaker", volume=volume),
    )


@pytest.mark.parametrize("value", [0.0, 0.25, 1.0, 10.0])
def test_constructor_accepts_supported_linear_volume(tmp_path: Path, value: float) -> None:
    assert [pipeline.volume for pipeline in _pipelines(tmp_path, volume=value)] == [value, value, value]


@pytest.mark.parametrize("value", [True, "1", None, -0.01, 10.01, float("nan"), float("inf")])
def test_constructor_rejects_invalid_volume(tmp_path: Path, value: object) -> None:
    with pytest.raises((TypeError, ValueError), match="volume"):
        _pipelines(tmp_path, volume=cast(Any, value))


def test_volume_can_be_changed_before_start(tmp_path: Path) -> None:
    for pipeline in _pipelines(tmp_path):
        pipeline.set_volume(2.0)
        assert pipeline.volume == 2.0


def test_running_volume_change_updates_the_active_element(tmp_path: Path) -> None:
    for pipeline in _pipelines(tmp_path):
        runtime = _Runtime(PipelineState.RUNNING)
        element = Mock()
        pipeline._runtime = runtime
        pipeline._volume_element = element

        pipeline.set_volume(0.5)

        assert pipeline.volume == 0.5
        assert runtime.operations == 1
        element.set_volume.assert_called_once_with(0.5)


def test_volume_is_applied_to_each_replacement_element(tmp_path: Path) -> None:
    pipeline = MicrophoneCapturePipeline(
        logger=_logger(), handler=_Handler(), audio_format=AudioFormat(16_000, 1), device_id="microphone", volume=2.0
    )
    first = Mock()
    pipeline._set_volume_element(first)
    runtime = _Runtime(PipelineState.RUNNING)
    pipeline._runtime = cast(Any, runtime)
    pipeline.set_volume(0.25)
    replacement = Mock()
    pipeline._set_volume_element(replacement)

    first.set_volume.assert_has_calls([call(2.0), call(0.25)])
    replacement.set_volume.assert_called_once_with(0.25)


@pytest.mark.parametrize("state", [PipelineState.STARTING, PipelineState.STOPPING, PipelineState.STOPPED])
def test_volume_change_rejects_non_configurable_states(tmp_path: Path, state: PipelineState) -> None:
    pipeline = SpeakerPlaybackPipeline(
        logger=_logger(), audio_format=AudioFormat(16_000, 1), device_id="speaker", volume=1.0
    )
    pipeline._runtime = cast(Any, _Runtime(state))

    with pytest.raises(PipelineStateError, match=state.value):
        pipeline.set_volume(0.5)

    assert pipeline.volume == 1.0
