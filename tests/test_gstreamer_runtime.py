from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from lumivox_devicelab._gstreamer import runtime


def test_get_gst_loads_and_initializes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    gst = SimpleNamespace(init=Mock())
    gi = ModuleType("gi")
    require_version = Mock()
    setattr(gi, "require_version", require_version)
    repository = ModuleType("gi.repository")
    setattr(repository, "Gst", gst)

    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    monkeypatch.setattr(runtime, "_gst", None)

    assert runtime.get_gst() is gst
    assert runtime.get_gst() is gst

    require_version.assert_called_once_with("Gst", "1.0")
    gst.init.assert_called_once_with(None)


def test_get_gst_reports_missing_pygobject(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "gi", None)
    monkeypatch.delitem(sys.modules, "gi.repository", raising=False)
    monkeypatch.setattr(runtime, "_gst", None)

    with pytest.raises(runtime.GStreamerUnavailableError, match="PyGObject"):
        runtime.get_gst()
