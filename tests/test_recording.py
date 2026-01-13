from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, cast
from pathlib import Path
from unittest.mock import Mock

import pytest

from lumivox_devicelab.errors import PipelineTimeoutError
from lumivox_devicelab._gstreamer.graph import _PipelineGraph
from lumivox_devicelab._gstreamer.runtime import GStreamerElementError
from lumivox_devicelab._gstreamer.recording import recording_segment_path, validate_recording_path
from lumivox_devicelab._gstreamer.elements.flow import QueueOverflowPolicy


def test_recording_path_validation_and_generation_names(tmp_path: Path) -> None:
    base = validate_recording_path(tmp_path / "Capture.WAV", overwrite=False)

    assert base == tmp_path / "Capture.WAV"
    assert recording_segment_path(base, 0) == base
    assert recording_segment_path(base, 1) == tmp_path / "Capture.1.WAV"
    assert recording_segment_path(base, 12) == tmp_path / "Capture.12.WAV"


@pytest.mark.parametrize("suffix", (".mp3", "", ".wav.tmp"))
def test_recording_rejects_unsupported_extensions(tmp_path: Path, suffix: str) -> None:
    with pytest.raises(ValueError, match=".wav or .flac"):
        validate_recording_path(tmp_path / f"capture{suffix}", overwrite=False)


def test_recording_rejects_missing_parent_and_existing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="parent directory"):
        validate_recording_path(tmp_path / "missing" / "capture.wav", overwrite=False)

    existing = tmp_path / "capture.flac"
    existing.write_bytes(b"keep")
    with pytest.raises(ValueError, match="already exists"):
        validate_recording_path(existing, overwrite=False)

    assert validate_recording_path(existing, overwrite=True) == existing
    assert existing.read_bytes() == b"keep"


def test_recording_finalization_timeout_is_a_domain_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lumivox_devicelab._gstreamer import recording as recording_module

    queue = Mock()
    queue.impl.send_event.return_value = True
    logger = Mock()
    logger.bind.return_value = logger
    branch = recording_module._RecordingBranch(
        logger=logger,
        path=tmp_path / "capture.wav",
        temporary_path=tmp_path / ".capture.wav.tmp",
        queue=queue,
        encoder=Mock(),
        sink=Mock(),
        bus=Mock(timed_pop_filtered=Mock(return_value=None)),
        overwrite=False,
    )
    branch.mark_active()
    monkeypatch.setattr(
        recording_module,
        "get_gst",
        lambda: SimpleNamespace(
            Event=SimpleNamespace(new_eos=lambda: object()),
            MessageType=SimpleNamespace(ELEMENT=1, ERROR=4),
        ),
    )

    with pytest.raises(PipelineTimeoutError, match="recording finalization timed out"):
        branch.finalize(time.monotonic() + 0.01)

    queue.impl.send_event.assert_called_once()


def test_recording_branch_is_bounded_blocking_and_discards_an_unstarted_reservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lumivox_devicelab._gstreamer import recording as recording_module

    created_queue = Mock()
    queue_factory = Mock(return_value=created_queue)
    encoder = Mock()
    sink_pad = Mock()
    sink = Mock()
    sink.impl.get_static_pad.return_value = sink_pad
    monkeypatch.setattr(recording_module, "AudioQueue", queue_factory)
    monkeypatch.setattr(recording_module, "WavEnc", Mock(return_value=encoder))
    monkeypatch.setattr(recording_module, "FileSink", Mock(return_value=sink))
    monkeypatch.setattr(
        recording_module,
        "get_gst",
        lambda: SimpleNamespace(
            EventType=SimpleNamespace(EOS=1),
            PadProbeReturn=SimpleNamespace(OK=1),
            PadProbeType=SimpleNamespace(EVENT_DOWNSTREAM=1),
        ),
    )
    graph = Mock(spec=_PipelineGraph)
    path = tmp_path / "capture.wav"

    branch = recording_module._RecordingBranch.build(
        graph=graph,
        tee=cast(Any, Mock()),
        logger=Mock(bind=Mock(return_value=Mock())),
        path=path,
        overwrite=False,
    )

    temporary_path = cast(Path, cast(Any, branch)._temporary_path)
    assert temporary_path.exists()
    assert not path.exists()
    queue_factory.assert_called_once_with(
        max_time_ms=1_000,
        overflow_policy=QueueOverflowPolicy.BLOCK,
        name="recording-queue",
    )
    graph.branch.assert_called_once()
    branch.finalize(time.monotonic() + 1)
    assert not temporary_path.exists()


@pytest.mark.parametrize("overwrite", (False, True))
def test_recording_is_published_atomically_without_replacing_unless_explicit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, overwrite: bool
) -> None:
    from lumivox_devicelab._gstreamer import recording as recording_module

    target = tmp_path / "capture.wav"
    target.write_bytes(b"old")
    temporary = tmp_path / ".capture.wav.owned.tmp"
    temporary.write_bytes(b"new")
    sink = Mock()
    child = SimpleNamespace(type=2, src=sink.impl)
    structure = Mock()
    structure.get_name.return_value = "GstBinForwarded"
    structure.get_value.return_value = child
    message = Mock()
    message.type = 1
    message.get_structure.return_value = structure
    bus = Mock()
    bus.timed_pop_filtered.return_value = message
    queue = Mock()
    queue.impl.send_event.return_value = True
    logger = Mock()
    logger.bind.return_value = logger
    monkeypatch.setattr(
        recording_module,
        "get_gst",
        lambda: SimpleNamespace(
            Event=SimpleNamespace(new_eos=lambda: object()),
            MessageType=SimpleNamespace(ELEMENT=1, EOS=2, ERROR=4),
        ),
    )
    branch = recording_module._RecordingBranch(
        logger=logger,
        path=target,
        temporary_path=temporary,
        queue=queue,
        encoder=Mock(),
        sink=sink,
        bus=bus,
        overwrite=overwrite,
    )
    branch.mark_active()

    if overwrite:
        branch.finalize(time.monotonic() + 1)
        assert target.read_bytes() == b"new"
        assert not temporary.exists()
    else:
        with pytest.raises(GStreamerElementError, match="failed to publish"):
            branch.finalize(time.monotonic() + 1)
        assert target.read_bytes() == b"old"
        assert temporary.read_bytes() == b"new"
