from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, call

import pytest

from lumivox_devicelab import DeviceError, DeviceNotFoundError
from lumivox_devicelab.devices import AudioDevice, DeviceSnapshot, SpeakerDeviceDiscovery, MicrophoneDeviceDiscovery
from lumivox_devicelab._gstreamer import device_discovery
from lumivox_devicelab._gstreamer.device_discovery import (
    DeviceDirection,
    _ProbedDevice,
    resolve_pipewire_target,
)


class FakeStructure:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def has_field(self, field: str) -> bool:
        return field in self.values

    def get_value(self, field: str) -> object:
        return self.values[field]


class FakeDevice:
    def __init__(self, device_class: str, properties: dict[str, object]) -> None:
        self.device_class = device_class
        self.properties = FakeStructure(properties)

    def get_device_class(self) -> str:
        return self.device_class

    def get_properties(self) -> FakeStructure:
        return self.properties

    def get_caps(self) -> None:
        return None


class FakeProvider:
    def __init__(self, devices: list[FakeDevice], *, starts: bool = True) -> None:
        self.devices = devices
        self.starts = starts
        self.stopped = False

    def start(self) -> bool:
        return self.starts

    def get_devices(self) -> list[FakeDevice]:
        return self.devices

    def stop(self) -> None:
        self.stopped = True


def _logger() -> Mock:
    logger = Mock()
    logger.bind.return_value = logger
    return logger


def _install_provider(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    factory = SimpleNamespace(get=lambda: provider)
    gst = SimpleNamespace(DeviceProviderFactory=SimpleNamespace(find=lambda name: factory))
    monkeypatch.setattr(device_discovery, "get_gst", lambda: gst)
    monkeypatch.setattr(device_discovery, "_parse_caps", lambda *args, **kwargs: ())


def test_provider_filters_direction_copies_metadata_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider(
        [
            FakeDevice(
                "Audio/Source",
                {"node.name": "mic", "node.description": "Microphone", "object.serial": "0012", "is-default": True},
            ),
            FakeDevice(
                "Audio/Sink",
                {"node.name": "speaker", "node.description": "Speaker", "object.serial": "13"},
            ),
            FakeDevice("Video/Source", {}),
        ]
    )
    _install_provider(monkeypatch, provider)

    probed = device_discovery._probe_pipewire_devices(_logger())

    assert [(item.direction, item.device.id, item.target_object) for item in probed] == [
        (DeviceDirection.MICROPHONE, "mic", "12"),
        (DeviceDirection.SPEAKER, "speaker", "13"),
    ]
    assert provider.stopped


@pytest.mark.parametrize(
    "properties",
    [
        {"node.description": "Missing id"},
        {"node.name": "", "node.description": "Empty id"},
        {"node.name": "id"},
        {"node.name": "id", "node.description": 3},
        {"node.name": "id", "node.description": "Name", "is-default": "true"},
    ],
)
def test_malformed_audio_metadata_is_device_error(
    monkeypatch: pytest.MonkeyPatch, properties: dict[str, object]
) -> None:
    provider = FakeProvider([FakeDevice("Audio/Source", properties)])
    _install_provider(monkeypatch, provider)
    with pytest.raises(DeviceError):
        device_discovery._probe_pipewire_devices(_logger())
    assert provider.stopped


def test_snapshot_detects_duplicate_ids_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = _logger()
    duplicate = AudioDevice("same", "One", False, ())
    monkeypatch.setattr(
        device_discovery,
        "_probe_pipewire_devices",
        lambda logger: (
            _ProbedDevice(DeviceDirection.MICROPHONE, duplicate, "1"),
            _ProbedDevice(DeviceDirection.MICROPHONE, AudioDevice("same", "Two", False, ()), "2"),
        ),
    )
    with pytest.raises(DeviceError, match="duplicate"):
        device_discovery.snapshot_devices(DeviceDirection.MICROPHONE, logger)

    monkeypatch.setattr(
        device_discovery,
        "_probe_pipewire_devices",
        lambda logger: (
            _ProbedDevice(DeviceDirection.SPEAKER, AudioDevice("one", "One", True, ()), "1"),
            _ProbedDevice(DeviceDirection.SPEAKER, AudioDevice("two", "Two", True, ()), "2"),
        ),
    )
    with pytest.raises(DeviceError, match="multiple default"):
        device_discovery.snapshot_devices(DeviceDirection.SPEAKER, logger)


def test_resolver_uses_fresh_probe_and_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def probe(logger: Any) -> tuple[_ProbedDevice, ...]:
        nonlocal calls
        calls += 1
        return (
            _ProbedDevice(DeviceDirection.MICROPHONE, AudioDevice("mic", "Mic", True, ()), "44"),
            _ProbedDevice(DeviceDirection.SPEAKER, AudioDevice("speaker", "Speaker", True, ()), "45"),
        )

    monkeypatch.setattr(device_discovery, "_probe_pipewire_devices", probe)
    logger = _logger()
    assert resolve_pipewire_target("mic", DeviceDirection.MICROPHONE, logger) == "44"
    assert resolve_pipewire_target("mic", DeviceDirection.MICROPHONE, logger) == "44"
    assert calls == 2
    with pytest.raises(DeviceNotFoundError):
        resolve_pipewire_target("speaker", DeviceDirection.MICROPHONE, logger)
    with pytest.raises(DeviceNotFoundError):
        resolve_pipewire_target("missing", DeviceDirection.MICROPHONE, logger)


def test_resolver_rejects_missing_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        device_discovery,
        "_probe_pipewire_devices",
        lambda logger: (_ProbedDevice(DeviceDirection.MICROPHONE, AudioDevice("mic", "Mic", False, ()), None),),
    )
    with pytest.raises(DeviceError, match="object.serial"):
        resolve_pipewire_target("mic", DeviceDirection.MICROPHONE, _logger())


def test_public_clients_bind_logger_and_select_direction(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = _logger()
    seen: list[DeviceDirection] = []

    expected = DeviceSnapshot((), None)

    def snapshot(direction: DeviceDirection, bound_logger: Any) -> DeviceSnapshot:
        seen.append(direction)
        return expected

    monkeypatch.setattr(device_discovery, "snapshot_devices", snapshot)
    assert MicrophoneDeviceDiscovery(logger=logger).snapshot() is expected
    assert SpeakerDeviceDiscovery(logger=logger).snapshot() is expected
    assert seen == [DeviceDirection.MICROPHONE, DeviceDirection.SPEAKER]
    assert logger.bind.call_args_list == [call(module="devicelab"), call(module="devicelab")]
