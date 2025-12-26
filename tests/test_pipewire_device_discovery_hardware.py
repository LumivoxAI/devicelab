from __future__ import annotations

import os
from unittest.mock import Mock

import pytest
from conftest import SPEAKER_ID_ENV, MICROPHONE_ID_ENV

from lumivox_devicelab import SpeakerDeviceDiscovery, MicrophoneDeviceDiscovery
from lumivox_devicelab._gstreamer.device_discovery import DeviceDirection, resolve_pipewire_target


@pytest.mark.pipewire_hardware(device="microphone")
def test_microphone_snapshot_and_resolution() -> None:
    _assert_snapshot_and_resolution(MICROPHONE_ID_ENV, DeviceDirection.MICROPHONE)


@pytest.mark.pipewire_hardware(device="speaker")
def test_speaker_snapshot_and_resolution() -> None:
    _assert_snapshot_and_resolution(SPEAKER_ID_ENV, DeviceDirection.SPEAKER)


def _assert_snapshot_and_resolution(environment_name: str, direction: DeviceDirection) -> None:
    device_id = os.environ[environment_name]
    logger = Mock()
    logger.bind.return_value = logger
    if direction is DeviceDirection.MICROPHONE:
        snapshot = MicrophoneDeviceDiscovery(logger=logger).snapshot()
    else:
        snapshot = SpeakerDeviceDiscovery(logger=logger).snapshot()
    assert tuple(device.id for device in snapshot.devices).count(device_id) == 1
    target = resolve_pipewire_target(device_id, direction, logger)
    assert target.isdecimal()
