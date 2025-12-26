from __future__ import annotations

from unittest.mock import Mock

import pytest

from lumivox_devicelab import IntRange, ByteOrder, PcmSampleKind
from lumivox_devicelab._gstreamer.runtime import get_gst
from lumivox_devicelab._gstreamer.device_discovery import _parse_caps

pytestmark = pytest.mark.gstreamer


def test_caps_parser_preserves_lists_ranges_formats_and_structure_correlation() -> None:
    gst = get_gst()
    caps = gst.Caps.from_string(
        "audio/x-raw,format=(string){S16LE,F32LE},rate=(int){16000,48000},channels=(int)[1,2],layout=interleaved;"
        "audio/x-raw,format=S24_32LE,rate=96000,channels=4,layout=interleaved"
    )
    logger = Mock()

    capabilities = _parse_caps(caps, device_id="test", logger=logger)

    assert [(item.format.kind, item.format.significant_bits, item.format.storage_bits) for item in capabilities] == [
        (PcmSampleKind.SIGNED_INTEGER, 16, 16),
        (PcmSampleKind.FLOAT, 32, 32),
        (PcmSampleKind.SIGNED_INTEGER, 24, 32),
    ]
    assert capabilities[0].sample_rates == (16_000, 48_000)
    assert capabilities[0].channel_counts == (IntRange(1, 2),)
    assert capabilities[2].sample_rates == (96_000,)
    assert capabilities[2].channel_counts == (4,)


@pytest.mark.parametrize(
    ("format_name", "kind", "bits", "storage", "order"),
    [
        ("S8", PcmSampleKind.SIGNED_INTEGER, 8, 8, ByteOrder.NOT_APPLICABLE),
        ("U16BE", PcmSampleKind.UNSIGNED_INTEGER, 16, 16, ByteOrder.BIG),
        ("S20LE", PcmSampleKind.SIGNED_INTEGER, 20, 24, ByteOrder.LITTLE),
        ("F64BE", PcmSampleKind.FLOAT, 64, 64, ByteOrder.BIG),
    ],
)
def test_caps_parser_maps_common_pcm_formats(
    format_name: str, kind: PcmSampleKind, bits: int, storage: int, order: ByteOrder
) -> None:
    caps = get_gst().Caps.from_string(f"audio/x-raw,format={format_name},rate=48000,channels=2,layout=interleaved")
    capability = _parse_caps(caps, device_id="test", logger=Mock())[0]
    assert (capability.format.kind, capability.format.significant_bits, capability.format.storage_bits) == (
        kind,
        bits,
        storage,
    )
    assert capability.format.byte_order is order


def test_unknown_caps_are_omitted_with_structured_diagnostic() -> None:
    caps = get_gst().Caps.from_string(
        "audio/mpeg,mpegversion=1;audio/x-raw,format=UNKNOWN,rate=48000,channels=2;"
        "audio/x-raw,format=S16LE,rate=48000,channels=2,layout=non-interleaved"
    )
    logger = Mock()
    assert _parse_caps(caps, device_id="test", logger=logger) == ()
    assert logger.warning.call_count == 3
    assert all(call.args[0] == "device_capability_omitted" for call in logger.warning.call_args_list)
