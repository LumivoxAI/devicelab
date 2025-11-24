"""GStreamer loading and common implementation errors."""

from __future__ import annotations

from typing import Any


class GStreamerUnavailableError(RuntimeError):
    """Raised when the required PyGObject GStreamer bindings are unavailable."""


class GStreamerElementError(RuntimeError):
    """Raised when an internal GStreamer element cannot be created or linked."""


def get_gst() -> Any:
    """Load and initialize GStreamer only when an element is constructed."""
    try:
        import gi  # type: ignore[import-not-found]

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # type: ignore[import-not-found]
    except (ImportError, ValueError) as error:
        raise GStreamerUnavailableError(
            "PyGObject with GStreamer 1.0 introspection bindings is required. "
            "Install the system GStreamer and PyGObject packages."
        ) from error

    Gst.init(None)
    return Gst
