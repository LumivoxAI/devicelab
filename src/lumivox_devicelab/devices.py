"""Public audio-device discovery contracts."""

from __future__ import annotations

from enum import StrEnum
from dataclasses import dataclass
from collections.abc import Iterable

from lumivox_core.logger import Logger

from lumivox_devicelab._validation import validate_gst_positive_int


class PcmSampleKind(StrEnum):
    SIGNED_INTEGER = "signed_integer"
    UNSIGNED_INTEGER = "unsigned_integer"
    FLOAT = "float"


class ByteOrder(StrEnum):
    LITTLE = "little"
    BIG = "big"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class PcmFormat:
    kind: PcmSampleKind
    significant_bits: int
    storage_bits: int
    byte_order: ByteOrder

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PcmSampleKind):
            raise TypeError("kind must be a PcmSampleKind")
        significant_bits = validate_gst_positive_int(self.significant_bits, name="significant_bits")
        storage_bits = validate_gst_positive_int(self.storage_bits, name="storage_bits")
        if storage_bits < significant_bits:
            raise ValueError("storage_bits must be greater than or equal to significant_bits")
        if not isinstance(self.byte_order, ByteOrder):
            raise TypeError("byte_order must be a ByteOrder")


@dataclass(frozen=True, slots=True)
class IntRange:
    """An inclusive stepped range of positive GStreamer integers."""

    minimum: int
    maximum: int
    step: int = 1

    def __post_init__(self) -> None:
        minimum = validate_gst_positive_int(self.minimum, name="minimum")
        maximum = validate_gst_positive_int(self.maximum, name="maximum")
        step = validate_gst_positive_int(self.step, name="step")
        if maximum < minimum:
            raise ValueError("maximum must be greater than or equal to minimum")
        if (maximum - minimum) % step:
            raise ValueError("maximum must be reachable from minimum using step")


IntChoice = int | IntRange


def _validate_choices(values: object, *, name: str) -> tuple[IntChoice, ...]:
    if not isinstance(values, Iterable):
        raise TypeError(f"{name} must be an iterable")
    result = tuple(values)
    if not result:
        raise ValueError(f"{name} must not be empty")
    for value in result:
        if isinstance(value, IntRange):
            continue
        validate_gst_positive_int(value, name=f"{name} entries")
    return result


@dataclass(frozen=True, slots=True)
class AudioCapability:
    format: PcmFormat
    sample_rates: tuple[IntChoice, ...]
    channel_counts: tuple[IntChoice, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.format, PcmFormat):
            raise TypeError("format must be a PcmFormat")
        object.__setattr__(self, "sample_rates", _validate_choices(self.sample_rates, name="sample_rates"))
        object.__setattr__(self, "channel_counts", _validate_choices(self.channel_counts, name="channel_counts"))


def _validate_non_empty_text(value: object, *, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True, slots=True)
class AudioDevice:
    id: str
    name: str
    is_default: bool
    capabilities: tuple[AudioCapability, ...]

    def __post_init__(self) -> None:
        _validate_non_empty_text(self.id, name="id")
        _validate_non_empty_text(self.name, name="name")
        if not isinstance(self.is_default, bool):
            raise TypeError("is_default must be a boolean")
        if not isinstance(self.capabilities, Iterable):
            raise TypeError("capabilities must be an iterable")
        capabilities = tuple(self.capabilities)
        if not all(isinstance(capability, AudioCapability) for capability in capabilities):
            raise TypeError("capabilities entries must be AudioCapability values")
        object.__setattr__(self, "capabilities", capabilities)


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    devices: tuple[AudioDevice, ...]
    default: AudioDevice | None

    def __post_init__(self) -> None:
        if not isinstance(self.devices, Iterable):
            raise TypeError("devices must be an iterable")
        devices = tuple(self.devices)
        if not all(isinstance(device, AudioDevice) for device in devices):
            raise TypeError("devices entries must be AudioDevice values")
        flagged = tuple(device for device in devices if device.is_default)
        if len(flagged) > 1:
            raise ValueError("devices must contain at most one default")
        if self.default is not None and not isinstance(self.default, AudioDevice):
            raise TypeError("default must be an AudioDevice or None")
        expected_default = flagged[0] if flagged else None
        if self.default != expected_default:
            raise ValueError("default must be the unique default member of devices")
        object.__setattr__(self, "devices", devices)


class MicrophoneDeviceDiscovery:
    """Produce immutable snapshots of PipeWire audio sources."""

    def __init__(self, *, logger: Logger) -> None:
        self._logger = logger.bind(module="devicelab")

    def snapshot(self) -> DeviceSnapshot:
        from lumivox_devicelab._gstreamer.device_discovery import DeviceDirection, snapshot_devices

        return snapshot_devices(DeviceDirection.MICROPHONE, self._logger)


class SpeakerDeviceDiscovery:
    """Produce immutable snapshots of PipeWire audio sinks."""

    def __init__(self, *, logger: Logger) -> None:
        self._logger = logger.bind(module="devicelab")

    def snapshot(self) -> DeviceSnapshot:
        from lumivox_devicelab._gstreamer.device_discovery import DeviceDirection, snapshot_devices

        return snapshot_devices(DeviceDirection.SPEAKER, self._logger)
