from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from lumivox_devicelab._gstreamer.audio import RawAudioSpec
from lumivox_devicelab._gstreamer.elements import app
from lumivox_devicelab._gstreamer.elements.app import (
    AppSink,
    AppSinkPolicy,
    CapturePacketError,
    CapturePacketFlags,
    CapturePacketErrorKind,
)


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


class FakeSegment:
    format = 1
    flags = 0
    rate = 1.0
    applied_rate = 1.0
    base = 0
    offset = 0
    start = 0
    stop = -1
    time = 0

    def to_running_time(self, format: object, pts: int) -> int:
        del format
        return pts


def _app_sink(spec: RawAudioSpec) -> AppSink:
    sink = object.__new__(AppSink)
    sink._spec = spec
    sink._segment_key = None
    return sink


def _sample(
    data: bytes | bytearray,
    *,
    spec: RawAudioSpec,
    pts: int = 123,
    mapped: bool = True,
    flags: set[int] | None = None,
) -> tuple[SimpleNamespace, Mock]:
    unmap = Mock()
    buffer = SimpleNamespace(
        pts=pts,
        map=Mock(return_value=(mapped, SimpleNamespace(size=len(data), data=data))),
        unmap=unmap,
        has_flags=lambda flag: flag in (flags or set()),
    )
    sample = SimpleNamespace(
        get_buffer=Mock(return_value=buffer),
        get_caps=Mock(return_value=FakeCaps(FakeStructure(rate=spec.rate, channels=spec.channels))),
        get_segment=Mock(return_value=FakeSegment()),
    )
    return sample, unmap


def test_appsink_converts_and_copies_pcm_data(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = RawAudioSpec(rate=16_000, channels=2)
    source = bytearray(np.array([[1, -2], [3, -4]], dtype="<i2").tobytes())
    sample, unmap = _sample(source, spec=spec)
    monkeypatch.setattr(
        app,
        "get_gst",
        lambda: SimpleNamespace(
            CLOCK_TIME_NONE=-1,
            Format=SimpleNamespace(TIME=object()),
            MapFlags=SimpleNamespace(READ=object()),
            BufferFlags=SimpleNamespace(DISCONT=1, GAP=2),
        ),
    )

    packet = _app_sink(spec)._to_packet(sample)
    source[0] = 0

    assert np.array_equal(packet.samples, np.array([[1, -2], [3, -4]], dtype=np.int16))
    assert packet.running_time_ns == 123
    assert packet.duration_ns == 125_000
    assert packet.flags is CapturePacketFlags.NONE
    unmap.assert_called_once()


def test_appsink_rejects_caps_mismatch() -> None:
    spec = RawAudioSpec(rate=16_000, channels=1)
    sample, _ = _sample(np.array([1], dtype="<i2").tobytes(), spec=spec)
    sample.get_caps.return_value = FakeCaps(FakeStructure(rate=48_000, channels=1))

    with pytest.raises(CapturePacketError, match="caps do not match") as raised:
        _app_sink(spec)._to_packet(sample)
    assert raised.value.kind is CapturePacketErrorKind.CAPS


def test_appsink_rejects_sample_without_buffer() -> None:
    spec = RawAudioSpec(rate=16_000, channels=1)
    sample, _ = _sample(np.array([1], dtype="<i2").tobytes(), spec=spec)
    sample.get_buffer.return_value = None

    with pytest.raises(CapturePacketError, match="has no buffer") as raised:
        _app_sink(spec)._to_packet(sample)
    assert raised.value.kind is CapturePacketErrorKind.MALFORMED


def test_appsink_rejects_malformed_frame_size(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = RawAudioSpec(rate=16_000, channels=2)
    sample, unmap = _sample(b"abc", spec=spec)
    monkeypatch.setattr(app, "get_gst", lambda: SimpleNamespace(MapFlags=SimpleNamespace(READ=object())))

    with pytest.raises(CapturePacketError, match="not a multiple") as raised:
        _app_sink(spec)._to_packet(sample)

    assert raised.value.kind is CapturePacketErrorKind.MALFORMED
    unmap.assert_called_once()


def test_appsink_rejects_mapping_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = RawAudioSpec(rate=16_000, channels=1)
    sample, _ = _sample(b"", spec=spec, mapped=False)
    monkeypatch.setattr(app, "get_gst", lambda: SimpleNamespace(MapFlags=SimpleNamespace(READ=object())))

    with pytest.raises(CapturePacketError, match="failed to map") as raised:
        _app_sink(spec)._to_packet(sample)
    assert raised.value.kind is CapturePacketErrorKind.MAPPING


def test_appsink_rejects_clock_time_none(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = RawAudioSpec(rate=16_000, channels=1)
    sample, _ = _sample(np.array([1], dtype="<i2").tobytes(), spec=spec, pts=-1)
    monkeypatch.setattr(
        app,
        "get_gst",
        lambda: SimpleNamespace(CLOCK_TIME_NONE=-1, MapFlags=SimpleNamespace(READ=object())),
    )

    with pytest.raises(CapturePacketError, match="has no PTS") as raised:
        _app_sink(spec)._to_packet(sample)
    assert raised.value.kind is CapturePacketErrorKind.MISSING_TIMESTAMP


def test_appsink_rejects_timestamp_when_segment_cannot_convert_pts(monkeypatch: pytest.MonkeyPatch) -> None:
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

    with pytest.raises(CapturePacketError, match="cannot be converted") as raised:
        _app_sink(spec)._to_packet(sample)
    assert raised.value.kind is CapturePacketErrorKind.MISSING_TIMESTAMP


def test_appsink_preserves_relevant_flags_and_marks_segment_change(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = RawAudioSpec(rate=16_000, channels=1)
    gst = SimpleNamespace(
        CLOCK_TIME_NONE=-1,
        Format=SimpleNamespace(TIME=object()),
        MapFlags=SimpleNamespace(READ=object()),
        BufferFlags=SimpleNamespace(DISCONT=1, GAP=2),
    )
    monkeypatch.setattr(app, "get_gst", lambda: gst)
    sink = _app_sink(spec)
    first, _ = _sample(np.array([1], dtype="<i2").tobytes(), spec=spec, flags={gst.BufferFlags.GAP})
    second, _ = _sample(np.array([2], dtype="<i2").tobytes(), spec=spec)
    second_segment = FakeSegment()
    second_segment.start = 42
    second.get_segment.return_value = second_segment

    first_packet = sink._to_packet(first)
    second_packet = sink._to_packet(second)

    assert first_packet.flags == CapturePacketFlags.GAP
    assert second_packet.flags == CapturePacketFlags.DISCONT


@pytest.mark.gstreamer(factories=("appsink",))
def test_appsink_policies_are_bounded_and_select_drop_behavior() -> None:
    spec = RawAudioSpec(rate=16_000, channels=1)
    live = AppSink(spec, policy=AppSinkPolicy.LIVE_DROP, max_buffers=3)
    batch = AppSink(spec, policy=AppSinkPolicy.BATCH_BLOCK, max_buffers=5)

    assert live.impl.get_property("max-buffers") == 3
    assert live.impl.get_property("drop") is True
    assert batch.impl.get_property("max-buffers") == 5
    assert batch.impl.get_property("drop") is False
