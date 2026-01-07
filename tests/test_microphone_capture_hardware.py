from __future__ import annotations

import os
from threading import Event
from unittest.mock import Mock

import numpy as np
import pytest
from conftest import MICROPHONE_ID_ENV

from lumivox_devicelab.errors import PipelineError
from lumivox_devicelab.capture import CapturedChunk, CaptureContext, CaptureHandler
from lumivox_devicelab.formats import AudioFormat
from lumivox_devicelab.microphone import MicrophoneCapturePipeline


class HardwareHandler(CaptureHandler):
    def __init__(self) -> None:
        self.received = Event()
        self.chunk: CapturedChunk | None = None
        self.stop_calls = 0
        self.stop_cause: PipelineError | None = None

    def on_chunk(self, chunk: CapturedChunk) -> None:
        self.chunk = chunk
        self.received.set()

    def on_stop(self, context: CaptureContext, cause: PipelineError | None) -> None:
        del context
        self.stop_calls += 1
        self.stop_cause = cause


@pytest.mark.pipewire_hardware(device="microphone")
def test_configured_microphone_captures_valid_pcm() -> None:
    logger = Mock()
    logger.bind.return_value = logger
    handler = HardwareHandler()
    pipeline = MicrophoneCapturePipeline(
        logger=logger,
        handler=handler,
        audio_format=AudioFormat(16_000, 1),
        device_id=os.environ[MICROPHONE_ID_ENV],
    )

    try:
        pipeline.start()
        assert handler.received.wait(5)
    finally:
        pipeline.stop()

    assert handler.chunk is not None
    assert handler.chunk.samples.dtype == np.dtype("<i2")
    assert handler.chunk.samples.ndim == 1
    assert handler.chunk.samples.size > 0
    assert handler.stop_calls == 1
    assert handler.stop_cause is None
