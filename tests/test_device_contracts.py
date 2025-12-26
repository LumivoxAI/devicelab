from __future__ import annotations

from typing import Any
from dataclasses import FrozenInstanceError

import pytest

from lumivox_devicelab import (
    IntRange,
    ByteOrder,
    PcmFormat,
    AudioDevice,
    PcmSampleKind,
    DeviceSnapshot,
    AudioCapability,
)


def _format() -> PcmFormat:
    return PcmFormat(PcmSampleKind.SIGNED_INTEGER, 16, 16, ByteOrder.LITTLE)


def _capability() -> AudioCapability:
    return AudioCapability(_format(), (16_000, IntRange(44_100, 48_000, 3_900)), (1, 2))


def test_device_enums_have_exact_values() -> None:
    assert [kind.value for kind in PcmSampleKind] == ["signed_integer", "unsigned_integer", "float"]
    assert [order.value for order in ByteOrder] == ["little", "big", "not_applicable"]


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ((PcmSampleKind.SIGNED_INTEGER, 0, 16, ByteOrder.LITTLE), "significant_bits"),
        ((PcmSampleKind.SIGNED_INTEGER, 24, 16, ByteOrder.LITTLE), "storage_bits"),
        (("signed_integer", 16, 16, ByteOrder.LITTLE), "kind"),
        ((PcmSampleKind.SIGNED_INTEGER, 16, 16, "little"), "byte_order"),
    ],
)
def test_pcm_format_rejects_invalid_values(values: tuple[Any, ...], message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        PcmFormat(*values)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ((0, 1, 1), "minimum"),
        ((2, 1, 1), "maximum"),
        ((1, 2, 0), "step"),
        ((1, 4, 2), "reachable"),
    ],
)
def test_int_range_rejects_invalid_values(values: tuple[int, int, int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        IntRange(*values)


def test_capability_copies_iterables_and_range_is_inclusive() -> None:
    rates: list[int | IntRange] = [16_000, IntRange(44_100, 48_000, 3_900)]
    capability = AudioCapability(_format(), rates, [1, 2])  # type: ignore[arg-type]
    rates.clear()
    assert capability.sample_rates == (16_000, IntRange(44_100, 48_000, 3_900))
    assert capability.channel_counts == (1, 2)


@pytest.mark.parametrize("field", ["sample_rates", "channel_counts"])
def test_capability_rejects_empty_choices(field: str) -> None:
    values: dict[str, object] = {"format": _format(), "sample_rates": (16_000,), "channel_counts": (1,), field: ()}
    with pytest.raises(ValueError, match=field):
        AudioCapability(**values)  # type: ignore[arg-type]


def test_device_and_snapshot_are_immutable_defensive_copies() -> None:
    capabilities = [_capability()]
    device = AudioDevice("node", "Microphone", True, capabilities)  # type: ignore[arg-type]
    devices = [device]
    snapshot = DeviceSnapshot(devices, device)  # type: ignore[arg-type]
    capabilities.clear()
    devices.clear()

    assert snapshot.devices == (device,)
    assert device.capabilities == (_capability(),)
    with pytest.raises(FrozenInstanceError):
        device.name = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(("device_id", "name"), [("", "name"), ("id", "")])
def test_device_rejects_empty_identity_fields(device_id: str, name: str) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        AudioDevice(device_id, name, False, ())


def test_snapshot_requires_unique_consistent_default() -> None:
    first = AudioDevice("first", "First", True, ())
    second = AudioDevice("second", "Second", True, ())
    with pytest.raises(ValueError, match="at most one"):
        DeviceSnapshot((first, second), first)
    with pytest.raises(ValueError, match="unique default"):
        DeviceSnapshot((AudioDevice("plain", "Plain", False, ()),), first)
