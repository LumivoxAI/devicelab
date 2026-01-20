"""GStreamer file source, parser, encoder, and sink elements."""

from __future__ import annotations

import os
from os import PathLike

from .base import BaseElement


def _validate_location(location: str | PathLike[str]) -> str:
    value = os.fspath(location)
    if not value:
        raise ValueError("location must not be empty")
    return value


class FileSrc(BaseElement):
    def __init__(self, location: str | PathLike[str], name: str | None = None) -> None:
        validated_location = _validate_location(location)
        super().__init__("filesrc", name)
        self.impl.set_property("location", validated_location)


class FileSink(BaseElement):
    def __init__(self, location: str | PathLike[str], name: str | None = None) -> None:
        validated_location = _validate_location(location)
        super().__init__("filesink", name)
        self.impl.set_property("location", validated_location)
        self.impl.set_property("sync", False)


class WavParse(BaseElement):
    def __init__(self, name: str | None = None) -> None:
        super().__init__("wavparse", name)


class WavEnc(BaseElement):
    def __init__(self, name: str | None = None) -> None:
        super().__init__("wavenc", name)


class FlacParse(BaseElement):
    def __init__(self, name: str | None = None) -> None:
        super().__init__("flacparse", name)


class FlacDec(BaseElement):
    def __init__(self, name: str | None = None) -> None:
        super().__init__("flacdec", name)


class FlacEnc(BaseElement):
    def __init__(self, quality: int = 5, name: str | None = None) -> None:
        if not 0 <= quality <= 9:
            raise ValueError("quality must be between 0 and 9")
        super().__init__("flacenc", name)
        self.impl.set_property("quality", quality)
