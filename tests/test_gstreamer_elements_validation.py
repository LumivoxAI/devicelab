from __future__ import annotations

from collections.abc import Callable

import pytest

from lumivox_devicelab._gstreamer.audio import RawAudioSpec
from lumivox_devicelab._gstreamer.elements.app import AppSrc
from lumivox_devicelab._gstreamer.elements.file import FileSrc, FlacEnc, FileSink
from lumivox_devicelab._gstreamer.elements.flow import AudioQueue
from lumivox_devicelab._gstreamer.elements.audio import AudioResample
from lumivox_devicelab._gstreamer.elements.pipewire import PipeWireSrc


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: AudioResample(quality=-1), "quality"),
        (lambda: AudioResample(quality=11), "quality"),
        (lambda: AudioQueue(max_time_ms=0), "max_time_ms"),
        (lambda: FileSrc(""), "location"),
        (lambda: FileSink(""), "location"),
        (lambda: FlacEnc(quality=10), "quality"),
        (lambda: PipeWireSrc(target_object=""), "target_object"),
        (lambda: PipeWireSrc(min_buffers=0), "min_buffers"),
        (lambda: PipeWireSrc(min_buffers=3, max_buffers=2), "max_buffers"),
        (lambda: AppSrc(RawAudioSpec(rate=16_000, channels=1), max_queue_time_ms=0), "max_queue_time_ms"),
    ],
)
def test_element_constructor_validates_before_loading_gstreamer(factory: Callable[[], object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()
