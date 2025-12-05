from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from lumivox_devicelab._gstreamer.audio import RawAudioSpec
from lumivox_devicelab._gstreamer.runtime import GStreamerElementError
from lumivox_devicelab._gstreamer.elements import app
from lumivox_devicelab._gstreamer.elements.app import AppSink


class FakeStructure:
    def __init__(self, *, rate: int, channels: int, format: str = "S16LE", layout: str = "interleaved") -> None:
        self._rate = rate
        self._channels = channels
        self._format = format
        self._layout = layout

    def get_name(self) -> str:
        return "audio/x-raw"

    def get_string(self, name: str) -> str | None:
        return {"format": self._format, "layout": self._layout}.get(name)

    def get_int(self, name: str) -> tuple[bool, int]:
        return (True, self._rate) if name == "rate" else (True, self._channels)


class FakeCaps:
    def __init__(self, structure: FakeStructure) -> None:
        self._structure = structure

    def is_empty(self) -> bool:
        return False

    def is_any(self) -> bool:
        return False

    def get_structure(self, index: int) -> FakeStructure | None:
        return self._structure if index == 0 else None


def _app_sink(spec: RawAudioSpec) -> AppSink:
    sink = object.__new__(AppSink)
    sink._spec = spec
    return sink


def _sample(
    data: bytes | bytearray,
    *,
    spec: RawAudioSpec,
    pts: int = 123,
    mapped: bool = True,
) -> tuple[SimpleNamespace, Mock]:
    unmap = Mock()
    buffer = SimpleNamespace(
        pts=pts,
        map=Mock(return_value=(mapped, SimpleNamespace(size=len(data), data=data))),
        unmap=unmap,
    )
    sample = SimpleNamespace(
        get_buffer=Mock(return_value=buffer),
        get_caps=Mock(return_value=FakeCaps(FakeStructure(rate=spec.rate, channels=spec.channels))),
        get_segment=Mock(return_value=None),
    )
    return sample, unmap


def test_appsink_converts_and_copies_pcm_data(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = RawAudioSpec(rate=16_000, channels=2)
    source = bytearray(np.array([[1, -2], [3, -4]], dtype="<i2").tobytes())
    sample, unmap = _sample(source, spec=spec)
    monkeypatch.setattr(
        app,
        "get_gst",
        lambda: SimpleNamespace(CLOCK_TIME_NONE=-1, MapFlags=SimpleNamespace(READ=object())),
    )

    data, timestamp = _app_sink(spec)._to_array(sample)
    source[0] = 0

    assert data is not None
    assert np.array_equal(data, np.array([[1, -2], [3, -4]], dtype=np.int16))
    assert timestamp == 123
    unmap.assert_called_once()


def test_appsink_rejects_caps_mismatch() -> None:
    spec = RawAudioSpec(rate=16_000, channels=1)
    sample, _ = _sample(np.array([1], dtype="<i2").tobytes(), spec=spec)
    sample.get_caps.return_value = FakeCaps(FakeStructure(rate=48_000, channels=1))

    with pytest.raises(GStreamerElementError, match="caps do not match"):
        _app_sink(spec)._to_array(sample)


def test_appsink_rejects_sample_without_buffer() -> None:
    spec = RawAudioSpec(rate=16_000, channels=1)
    sample, _ = _sample(np.array([1], dtype="<i2").tobytes(), spec=spec)
    sample.get_buffer.return_value = None

    with pytest.raises(GStreamerElementError, match="has no buffer"):
        _app_sink(spec)._to_array(sample)


def test_appsink_rejects_malformed_frame_size(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = RawAudioSpec(rate=16_000, channels=2)
    sample, unmap = _sample(b"abc", spec=spec)
    monkeypatch.setattr(app, "get_gst", lambda: SimpleNamespace(MapFlags=SimpleNamespace(READ=object())))

    with pytest.raises(GStreamerElementError, match="not a multiple"):
        _app_sink(spec)._to_array(sample)

    unmap.assert_called_once()


def test_appsink_rejects_mapping_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = RawAudioSpec(rate=16_000, channels=1)
    sample, _ = _sample(b"", spec=spec, mapped=False)
    monkeypatch.setattr(app, "get_gst", lambda: SimpleNamespace(MapFlags=SimpleNamespace(READ=object())))

    with pytest.raises(GStreamerElementError, match="failed to map"):
        _app_sink(spec)._to_array(sample)


def test_appsink_returns_no_timestamp_for_clock_time_none(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = RawAudioSpec(rate=16_000, channels=1)
    sample, _ = _sample(np.array([1], dtype="<i2").tobytes(), spec=spec, pts=-1)
    monkeypatch.setattr(
        app,
        "get_gst",
        lambda: SimpleNamespace(CLOCK_TIME_NONE=-1, MapFlags=SimpleNamespace(READ=object())),
    )

    data, timestamp = _app_sink(spec)._to_array(sample)

    assert data is not None
    assert timestamp is None


def test_appsink_returns_no_timestamp_when_segment_cannot_convert_pts(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = RawAudioSpec(rate=16_000, channels=1)
    sample, _ = _sample(np.array([1], dtype="<i2").tobytes(), spec=spec)
    sample.get_segment.return_value = SimpleNamespace(to_running_time=Mock(return_value=-1))
    monkeypatch.setattr(
        app,
        "get_gst",
        lambda: SimpleNamespace(
            CLOCK_TIME_NONE=-1, Format=SimpleNamespace(TIME=object()), MapFlags=SimpleNamespace(READ=object())
        ),
    )

    data, timestamp = _app_sink(spec)._to_array(sample)

    assert data is not None
    assert timestamp is None
