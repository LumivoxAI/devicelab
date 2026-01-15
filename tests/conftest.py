from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from lumivox_devicelab._gstreamer.runtime import GStreamerUnavailableError, get_gst

MICROPHONE_ID_ENV = "LUMIVOX_DEVICELAB_MICROPHONE_ID"
SPEAKER_ID_ENV = "LUMIVOX_DEVICELAB_SPEAKER_ID"
REQUIRED_GSTREAMER_FACTORIES = (
    "appsrc",
    "appsink",
    "audioconvert",
    "audioresample",
    "queue",
    "clocksync",
    "tee",
    "filesink",
    "pipewiresrc",
    "pipewiresink",
    "wavparse",
    "wavenc",
    "flacparse",
    "flacdec",
    "flacenc",
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-pipewire-hardware",
        action="store_true",
        default=False,
        help="run opt-in tests that access configured PipeWire devices",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-pipewire-hardware"):
        return
    skip = pytest.mark.skip(reason="PipeWire hardware tests require --run-pipewire-hardware")
    for item in items:
        if "pipewire_hardware" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _require_marked_test_environment(request: pytest.FixtureRequest) -> Iterator[None]:
    gstreamer_marker = request.node.get_closest_marker("gstreamer")
    hardware_marker = request.node.get_closest_marker("pipewire_hardware")
    if gstreamer_marker is not None or hardware_marker is not None:
        try:
            gst = get_gst()
        except GStreamerUnavailableError as error:
            pytest.skip(f"controlled GStreamer test unavailable: {error}")
        if gst.version() < (1, 24, 0, 0):
            pytest.skip(f"controlled GStreamer tests require GStreamer >=1.24; found {gst.version_string()}")
        required_factories = (
            gstreamer_marker.kwargs.get("factories", REQUIRED_GSTREAMER_FACTORIES)
            if gstreamer_marker is not None
            else REQUIRED_GSTREAMER_FACTORIES
        )
        missing = [name for name in required_factories if gst.ElementFactory.find(name) is None]
        if missing:
            pytest.skip(f"controlled GStreamer tests require missing plugin factories: {', '.join(missing)}")

    if hardware_marker is not None and request.config.getoption("--run-pipewire-hardware"):
        device_kind = hardware_marker.kwargs.get("device")
        required_env = {
            "microphone": MICROPHONE_ID_ENV,
            "speaker": SPEAKER_ID_ENV,
        }.get(device_kind)
        if required_env is None:
            raise pytest.UsageError("pipewire_hardware marker requires device='microphone' or device='speaker'")
        if not os.environ.get(required_env):
            pytest.skip(f"PipeWire {device_kind} hardware test requires the {required_env} stable device ID")
    yield
