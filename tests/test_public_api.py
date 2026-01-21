from __future__ import annotations

import inspect

import lumivox_devicelab
from lumivox_devicelab.speaker import SpeakerPlaybackPipeline
from lumivox_devicelab.microphone import MicrophoneCapturePipeline
from lumivox_devicelab.file_capture import FileReplayMode, FileCapturePipeline

EXPECTED_EXPORTS = [
    "AudioFormat",
    "ChannelSelection",
    "PipelineState",
    "DevicelabError",
    "DeviceError",
    "DeviceNotFoundError",
    "PipelineError",
    "PipelineStateError",
    "PipelineTimeoutError",
    "PlaybackSubmissionError",
    "CaptureContext",
    "CapturedChunk",
    "CaptureHandler",
    "PcmSampleKind",
    "ByteOrder",
    "PcmFormat",
    "IntRange",
    "AudioCapability",
    "AudioDevice",
    "DeviceSnapshot",
    "MicrophoneDeviceDiscovery",
    "SpeakerDeviceDiscovery",
    "FileReplayMode",
    "MicrophoneCapturePipeline",
    "FileCapturePipeline",
    "SpeakerPlaybackPipeline",
]


def test_public_exports_are_exact() -> None:
    assert lumivox_devicelab.__all__ == EXPECTED_EXPORTS
    assert all(hasattr(lumivox_devicelab, name) for name in EXPECTED_EXPORTS)
    assert lumivox_devicelab.FileReplayMode is FileReplayMode
    assert lumivox_devicelab.MicrophoneCapturePipeline is MicrophoneCapturePipeline
    assert lumivox_devicelab.FileCapturePipeline is FileCapturePipeline
    assert lumivox_devicelab.SpeakerPlaybackPipeline is SpeakerPlaybackPipeline


def test_internal_and_legacy_names_are_not_public() -> None:
    forbidden = {
        "AppSink",
        "AppSrc",
        "BaseElement",
        "CapturePipeline",
        "GStreamerElementError",
        "PipeWireSink",
        "PipeWireSrc",
        "PlaybackPipeline",
        "RawAudioSpec",
        "_PipelineGraph",
        "_PipelineRuntime",
    }

    assert forbidden.isdisjoint(lumivox_devicelab.__all__)
    assert all(not hasattr(lumivox_devicelab, name) for name in forbidden)


def test_public_pipeline_operation_defaults() -> None:
    for pipeline_type in (MicrophoneCapturePipeline, FileCapturePipeline, SpeakerPlaybackPipeline):
        assert inspect.signature(pipeline_type.start).parameters["timeout"].default == 10.0
        stop = inspect.signature(pipeline_type.stop).parameters
        assert stop["immediate"].default is False
        assert stop["timeout"].default == 10.0
        assert inspect.signature(pipeline_type.wait).parameters["timeout"].default is None
