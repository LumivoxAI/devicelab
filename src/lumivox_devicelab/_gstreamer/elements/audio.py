"""GStreamer audio conversion and caps elements."""

from __future__ import annotations

from .base import BaseElement
from ..audio import RawAudioSpec
from ..runtime import GStreamerElementError, get_gst


class SourceChannelCapsFilter(BaseElement):
    """Require an exact source channel count before explicit channel mapping."""

    def __init__(self, channels: int, name: str | None = None) -> None:
        super().__init__("capsfilter", name)
        self._channels = channels
        self.impl.set_property("caps", get_gst().Caps.from_string(f"audio/x-raw,channels={channels}"))

    def validate_negotiated_caps(self) -> bool:
        """Return false until caps exist and reject an incompatible fixed result."""
        pad = self.impl.get_static_pad("src")
        caps = None if pad is None else pad.get_current_caps()
        if caps is None or caps.is_empty() or caps.is_any():
            return False
        structure = caps.get_structure(0)
        success, channels = structure.get_int("channels")
        if structure.get_name() != "audio/x-raw" or not success or channels != self._channels:
            raise GStreamerElementError("negotiated source channels do not match channel selection")
        return True


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
