from __future__ import annotations

import os
import time
from unittest.mock import Mock

import numpy as np
import pytest
from conftest import SPEAKER_ID_ENV

from lumivox_devicelab.formats import AudioFormat
from lumivox_devicelab.speaker import SpeakerPlaybackPipeline

pytestmark = pytest.mark.pipewire_hardware(device="speaker")


def test_configured_speaker_accepts_and_stops_gracefully() -> None:
    logger = Mock()
    logger.bind.return_value = logger
    pipeline = SpeakerPlaybackPipeline(
        logger=logger,
        audio_format=AudioFormat(16_000, 1),
        device_id=os.environ[SPEAKER_ID_ENV],
    )
    frames = np.arange(1_600, dtype=np.float64)
    samples = (np.sin(2 * np.pi * 440 * frames / 16_000) * 1_000).astype(np.int16)

    pipeline.start()
    time.sleep(0.05)
    pipeline.submit(samples)
    pipeline.stop()

    assert pipeline.failure is None
