"""Base wrapper for GStreamer elements."""

from __future__ import annotations

from typing import Any

from ..runtime import GStreamerElementError, get_gst


class BaseElement:
    """An internal, eagerly-created GStreamer element wrapper."""

    def __init__(self, factory: str, name: str | None = None) -> None:
        self._factory = factory
        self._name = name
        gst = get_gst()
        if gst.ElementFactory.find(factory) is None:
            raise GStreamerElementError(
                f"Required GStreamer element factory '{factory}' is unavailable. Install the plugin that provides it."
            )
        self._impl: Any = gst.ElementFactory.make(factory, name)
        if self._impl is None:
            raise GStreamerElementError(f"Failed to create GStreamer element '{self}'")

    @property
    def impl(self) -> Any:
        """Return the implementation for internal pipeline construction only."""
        return self._impl

    @property
    def factory(self) -> str:
        return self._factory

    @property
    def name(self) -> str | None:
        return self._name

    def link(self, next_element: BaseElement) -> BaseElement:
        if not self.impl.link(next_element.impl):
            raise GStreamerElementError(f"Failed to link {self} -> {next_element}")
        return next_element

    def __str__(self) -> str:
        return f"{self.factory}:{self.name}" if self.name is not None else self.factory

    def __repr__(self) -> str:
        return str(self)
