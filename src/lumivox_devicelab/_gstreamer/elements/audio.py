"""GStreamer audio conversion and caps elements."""

from __future__ import annotations

from .base import BaseElement
from ..audio import RawAudioSpec
from ..runtime import get_gst


class AudioConvert(BaseElement):
    """Convert raw audio with Devicelab's fixed quantization policy."""

    def __init__(self, spec: RawAudioSpec, name: str | None = None) -> None:
        super().__init__("audioconvert", name)
        self.impl.set_property("dithering", "none")
        self.impl.set_property("noise-shaping", "none")
        if spec.channel_map is not None:
            assert spec.source_channels is not None
            rows = []
            for source_channel in spec.channel_map:
                coefficients = [
                    "(float)1.0" if index == source_channel else "(float)0.0" for index in range(spec.source_channels)
                ]
                rows.append(f"<{', '.join(coefficients)}>")
            get_gst().util_set_object_arg(self.impl, "mix-matrix", f"<{', '.join(rows)}>")


class AudioResample(BaseElement):
    """Resample raw audio with GStreamer's default Kaiser resampler."""

    def __init__(self, quality: int = 4, name: str | None = None) -> None:
        if not 0 <= quality <= 10:
            raise ValueError("quality must be between 0 and 10")
        super().__init__("audioresample", name)
        self.impl.set_property("quality", quality)
        self.impl.set_property("resample-method", "kaiser")


class CapsFilter(BaseElement):
    def __init__(self, spec: RawAudioSpec, name: str | None = None) -> None:
        super().__init__("capsfilter", name)

        # interleaved = [L, R, L, R, ...]
        layout = "interleaved"
        caps = [
            "audio/x-raw",
            "format=S16LE",
            f"rate={spec.rate}",
            f"channels={spec.channels}",
            f"layout={layout}",
        ]

        self.impl.set_property("caps", get_gst().Caps.from_string(",".join(caps)))
