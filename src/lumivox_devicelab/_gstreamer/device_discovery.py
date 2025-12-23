"""PipeWire device probing and exact target resolution."""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from dataclasses import dataclass

from lumivox_core.logger import Logger

from lumivox_devicelab.errors import DeviceError, DeviceNotFoundError
from lumivox_devicelab.devices import (
    IntRange,
    ByteOrder,
    PcmFormat,
    AudioDevice,
    PcmSampleKind,
    DeviceSnapshot,
    AudioCapability,
)
from lumivox_devicelab._validation import GST_INT_MAX
from lumivox_devicelab._gstreamer.runtime import get_gst, get_gst_audio


class DeviceDirection(StrEnum):
    MICROPHONE = "microphone"
    SPEAKER = "speaker"


_DEVICE_CLASSES = {
    DeviceDirection.MICROPHONE: "Audio/Source",
    DeviceDirection.SPEAKER: "Audio/Sink",
}


@dataclass(frozen=True, slots=True)
class _ProbedDevice:
    direction: DeviceDirection
    device: AudioDevice
    target_object: str | None


def _structure_value(structure: Any, field: str) -> Any | None:
    if not structure.has_field(field):
        return None
    return structure.get_value(field)


def _required_text(structure: Any, field: str) -> str:
    value = _structure_value(structure, field)
    if not isinstance(value, str) or not value:
        raise DeviceError(f"PipeWire device metadata {field!r} must be a non-empty string")
    return value


def _optional_target(structure: Any) -> str | None:
    value = _structure_value(structure, "object.serial")
    if value is None:
        return None
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        return None
    serial = int(value)
    if not 0 <= serial <= 2**64 - 1:
        return None
    return str(serial)


def _sequence_values(value: Any, value_type: Any) -> tuple[Any, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    if isinstance(value, value_type):
        return tuple(value[index] for index in range(len(value)))
    return (value,)


def _parse_int_choices(value: Any, gst: Any) -> tuple[int | IntRange, ...] | None:
    choices: list[int | IntRange] = []
    try:
        values = _sequence_values(value, gst.ValueList)
        for item in values:
            if isinstance(item, bool):
                return None
            if isinstance(item, int):
                if not 0 < item <= GST_INT_MAX:
                    return None
                choices.append(item)
            elif isinstance(item, range):
                choices.append(IntRange(item.start, item.stop, item.step))
            elif isinstance(item, gst.IntRange):
                range_value = item.range
                choices.append(IntRange(range_value.start, range_value.stop, range_value.step))
            else:
                return None
        return tuple(choices) or None
    except (AttributeError, TypeError, ValueError):
        return None


def _parse_pcm_format(name: Any, gst_audio: Any) -> PcmFormat | None:
    if not isinstance(name, str):
        return None
    try:
        audio_format = gst_audio.audio_format_from_string(name)
    except (TypeError, ValueError):
        return None
    if audio_format == gst_audio.AudioFormat.UNKNOWN:
        return None
    info = gst_audio.audio_format_get_info(audio_format)
    flags = info.flags
    if flags & gst_audio.AudioFormatFlags.FLOAT:
        kind = PcmSampleKind.FLOAT
    elif flags & gst_audio.AudioFormatFlags.INTEGER:
        kind = (
            PcmSampleKind.SIGNED_INTEGER
            if flags & gst_audio.AudioFormatFlags.SIGNED
            else PcmSampleKind.UNSIGNED_INTEGER
        )
    else:
        return None

    if info.endianness == 1234:
        byte_order = ByteOrder.LITTLE
    elif info.endianness == 4321:
        byte_order = ByteOrder.BIG
    elif info.endianness == 0:
        byte_order = ByteOrder.NOT_APPLICABLE
    else:
        return None
    return PcmFormat(kind, info.depth, info.width, byte_order)


def _log_omitted(logger: Logger, *, device_id: str, caps_index: int, reason: str) -> None:
    logger.warning(
        "device_capability_omitted",
        device_id=device_id,
        caps_index=caps_index,
        reason=reason,
    )


def _parse_caps(caps: Any, *, device_id: str, logger: Logger) -> tuple[AudioCapability, ...]:
    if caps is None or caps.is_any() or caps.is_empty():
        _log_omitted(logger, device_id=device_id, caps_index=-1, reason="caps are absent or unconstrained")
        return ()

    gst = get_gst()
    gst_audio = get_gst_audio()
    capabilities: list[AudioCapability] = []
    for index in range(caps.get_size()):
        structure = caps.get_structure(index)
        if structure.get_name() != "audio/x-raw":
            _log_omitted(logger, device_id=device_id, caps_index=index, reason="media type is not raw audio")
            continue
        try:
            layout = _structure_value(structure, "layout")
            rate_value = _structure_value(structure, "rate")
            channel_value = _structure_value(structure, "channels")
            format_value = _structure_value(structure, "format")
        except (AttributeError, TypeError, ValueError):
            _log_omitted(logger, device_id=device_id, caps_index=index, reason="caps fields cannot be decoded")
            continue
        if layout is not None and layout != "interleaved":
            _log_omitted(logger, device_id=device_id, caps_index=index, reason="layout is not interleaved")
            continue
        rates = _parse_int_choices(rate_value, gst)
        channels = _parse_int_choices(channel_value, gst)
        if rates is None or channels is None:
            _log_omitted(logger, device_id=device_id, caps_index=index, reason="rate or channels are unrepresentable")
            continue
        try:
            format_names = _sequence_values(format_value, gst.ValueList)
        except (AttributeError, TypeError):
            format_names = (format_value,)
        represented = False
        for format_name in format_names:
            pcm_format = _parse_pcm_format(format_name, gst_audio)
            if pcm_format is not None:
                capabilities.append(AudioCapability(pcm_format, rates, channels))
                represented = True
        if not represented:
            _log_omitted(logger, device_id=device_id, caps_index=index, reason="PCM format is unrepresentable")
    return tuple(capabilities)


def _probe_pipewire_devices(logger: Logger) -> tuple[_ProbedDevice, ...]:
    gst = get_gst()
    factory = gst.DeviceProviderFactory.find("pipewiredeviceprovider")
    if factory is None:
        raise DeviceError("GStreamer PipeWire device provider is unavailable")
    provider = factory.get()
    if provider is None:
        raise DeviceError("GStreamer PipeWire device provider could not be created")

    started = False
    try:
        if not provider.start():
            raise DeviceError("GStreamer PipeWire device provider could not be started")
        started = True
        result: list[_ProbedDevice] = []
        for gst_device in provider.get_devices():
            device_class = gst_device.get_device_class()
            direction = next((item for item, name in _DEVICE_CLASSES.items() if device_class == name), None)
            if direction is None:
                continue
            properties = gst_device.get_properties()
            if properties is None:
                raise DeviceError("PipeWire audio device has no metadata")
            device_id = _required_text(properties, "node.name")
            name = _required_text(properties, "node.description")
            default_value = _structure_value(properties, "is-default")
            if default_value is None:
                is_default = False
            elif isinstance(default_value, bool):
                is_default = default_value
            else:
                raise DeviceError("PipeWire device metadata 'is-default' must be a boolean")
            capabilities = _parse_caps(gst_device.get_caps(), device_id=device_id, logger=logger)
            result.append(
                _ProbedDevice(
                    direction,
                    AudioDevice(device_id, name, is_default, capabilities),
                    _optional_target(properties),
                )
            )
        return tuple(result)
    except DeviceError:
        raise
    except Exception as error:
        raise DeviceError("PipeWire device discovery failed") from error
    finally:
        if started:
            try:
                provider.stop()
            except Exception as error:
                logger.warning("device_provider_stop_failed", error=str(error))


def _matching_devices(direction: DeviceDirection, devices: tuple[_ProbedDevice, ...]) -> tuple[_ProbedDevice, ...]:
    matching = tuple(item for item in devices if item.direction is direction)
    ids: set[str] = set()
    for item in matching:
        if item.device.id in ids:
            raise DeviceError(f"PipeWire reported duplicate {direction.value} id {item.device.id!r}")
        ids.add(item.device.id)
    return matching


def snapshot_devices(direction: DeviceDirection, logger: Logger) -> DeviceSnapshot:
    matching = _matching_devices(direction, _probe_pipewire_devices(logger))
    defaults = tuple(item.device for item in matching if item.device.is_default)
    if len(defaults) > 1:
        raise DeviceError(f"PipeWire reported multiple default {direction.value} devices")
    snapshot = DeviceSnapshot(tuple(item.device for item in matching), defaults[0] if defaults else None)
    logger.debug(
        "device_snapshot_completed",
        direction=direction.value,
        device_count=len(snapshot.devices),
        default_device_id=snapshot.default.id if snapshot.default is not None else None,
    )
    return snapshot


def resolve_pipewire_target(device_id: str, direction: DeviceDirection, logger: Logger) -> str:
    """Resolve a stable node name to its current transient object serial."""
    if not isinstance(device_id, str):
        raise TypeError("device_id must be a string")
    if not device_id:
        raise ValueError("device_id must not be empty")
    matching = _matching_devices(direction, _probe_pipewire_devices(logger))
    selected = tuple(item for item in matching if item.device.id == device_id)
    if not selected:
        raise DeviceNotFoundError(f"{direction.value.capitalize()} device {device_id!r} was not found")
    if len(selected) > 1:
        raise DeviceError(f"PipeWire target {device_id!r} is ambiguous")
    target = selected[0].target_object
    if target is None:
        raise DeviceError(f"PipeWire device {device_id!r} has no valid object.serial")
    return target
