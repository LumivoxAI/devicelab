"""PipeWire source and sink elements."""

from __future__ import annotations

from .base import BaseElement
from ..runtime import get_gst


def _validate_optional_text(value: str | None, field_name: str) -> None:
    if value is not None and not value:
        raise ValueError(f"{field_name} must not be empty when provided")


class PipeWireSrc(BaseElement):
    def __init__(
        self,
        target_object: str | None = None,
        client_name: str | None = None,
        min_buffers: int = 2,
        max_buffers: int = 4,
        name: str | None = None,
    ) -> None:
        _validate_optional_text(target_object, "target_object")
        _validate_optional_text(client_name, "client_name")
        if min_buffers <= 0:
            raise ValueError("min_buffers must be positive")
        if max_buffers < min_buffers:
            raise ValueError("max_buffers must be greater than or equal to min_buffers")

        super().__init__("pipewiresrc", name)
        if target_object is not None:
            self.impl.set_property("target-object", target_object)
        if client_name is not None:
            self.impl.set_property("client-name", client_name)
        self.impl.set_property("do-timestamp", True)
        self.impl.set_property("use-bufferpool", False)
        self.impl.set_property("min-buffers", min_buffers)
        self.impl.set_property("max-buffers", max_buffers)

        props = [
            "props",
            "media.type=Audio",
            "media.category=Capture",
            "media.role=Communication",
        ]
        self.impl.set_property("stream-properties", get_gst().Structure.new_from_string(",".join(props)))


class PipeWireSink(BaseElement):
    def __init__(
        self,
        target_object: str | None = None,
        client_name: str | None = None,
        name: str | None = None,
    ) -> None:
        _validate_optional_text(target_object, "target_object")
        _validate_optional_text(client_name, "client_name")

        super().__init__("pipewiresink", name)
        self.impl.set_property("sync", True)
        if target_object is not None:
            self.impl.set_property("target-object", target_object)
        if client_name is not None:
            self.impl.set_property("client-name", client_name)

        props = [
            "props",
            "media.type=Audio",
            "media.category=Playback",
            "media.role=Communication",
        ]
        self.impl.set_property("stream-properties", get_gst().Structure.new_from_string(",".join(props)))
