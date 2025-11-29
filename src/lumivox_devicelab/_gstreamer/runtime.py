"""GStreamer loading and common implementation errors."""

from __future__ import annotations

from typing import Any
from threading import Lock

_gst: Any | None = None
_gst_lock = Lock()


class GStreamerUnavailableError(RuntimeError):
    """Raised when the required PyGObject GStreamer bindings are unavailable."""


class GStreamerElementError(RuntimeError):
    """Raised when an internal GStreamer element cannot be created or linked."""


def get_gst() -> Any:
    """Load and initialize GStreamer once, on first use."""
    global _gst
    if _gst is None:
        with _gst_lock:
            if _gst is None:
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
                _gst = Gst

    assert _gst is not None
    return _gst
