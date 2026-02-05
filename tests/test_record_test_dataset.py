from __future__ import annotations

import sys
import math
import wave
import argparse
import importlib.util
from typing import Any, cast
from pathlib import Path
from threading import Event, Thread, current_thread
from unittest.mock import Mock
from collections.abc import Callable

import numpy as np
import pytest
from lumivox_core.logger import Logger

from lumivox_devicelab import CapturedChunk

_TOOL_PATH = Path(__file__).parents[1] / "tools" / "record_test_dataset.py"
_SPEC = importlib.util.spec_from_file_location("record_test_dataset", _TOOL_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_TOOL: Any = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _TOOL
_SPEC.loader.exec_module(_TOOL)

Phrase = _TOOL.Phrase
Scenario = _TOOL.Scenario
SessionState = _TOOL.SessionState
DatasetController = _TOOL.DatasetController
_port = _TOOL._port
parse_scenario = _TOOL.parse_scenario
validate_empty_output_dir = _TOOL.validate_empty_output_dir


def _document(**updates: object) -> dict[str, object]:
    document: dict[str, object] = {
        "start_delay_seconds": 1,
        "phrase_delay_seconds": 2,
        "record_seconds": 3,
        "phrases": [{"id": "first", "text": "First"}, {"id": "second", "text": "Second"}],
    }
    document.update(updates)
    return document


def _scenario(*phrases: str, target_frames: int = 4) -> Any:
    return Scenario(
        start_delay_seconds=0.001,
        phrase_delay_seconds=0.001,
        record_seconds=target_frames / 16_000,
        phrases=tuple(Phrase(id=phrase, text=phrase.title()) for phrase in phrases),
        target_frames=target_frames,
    )


def _logger() -> Logger:
    logger = Mock(spec=Logger)
    logger.bind.return_value = logger
    return cast(Logger, logger)


def _chunk(values: list[int], *, generation: int = 0, discontinuity: bool = False) -> CapturedChunk:
    return CapturedChunk(
        samples=np.asarray(values, dtype="<i2"),
        generation=generation,
        captured_at_ns=0,
        running_time_ns=0,
        discontinuity=discontinuity,
    )


class _FakePipeline:
    def __init__(self, producer: Callable[[], None]) -> None:
        self._producer = producer
        self._thread: Thread | None = None
        self.immediate_stops: list[bool] = []

    def start(self) -> None:
        self._thread = Thread(target=self._producer)
        self._thread.start()

    def stop(self, *, immediate: bool = False) -> None:
        self.immediate_stops.append(immediate)
        if self._thread is not None and self._thread is not current_thread():
            self._thread.join(timeout=1)


def test_valid_scenario_preserves_phrase_order() -> None:
    scenario = parse_scenario(_document())

    assert [phrase.id for phrase in scenario.phrases] == ["first", "second"]
    assert scenario.target_frames == 48_000


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"phrases": []}, "missing field"),
        (_document(start_delay_seconds="1"), "must be a number"),
        (_document(phrase_delay_seconds=True), "must be a number"),
        (_document(record_seconds=math.inf), "finite and greater than zero"),
        (_document(record_seconds=0), "finite and greater than zero"),
        (_document(record_seconds=-1), "finite and greater than zero"),
        (_document(record_seconds=0.1 / 16_000), "too short"),
        (_document(phrases=[]), "non-empty array"),
        (_document(phrases=[{"id": "same", "text": "One"}, {"id": "same", "text": "Two"}]), "duplicate"),
        (_document(phrases=[{"id": "../escape", "text": "One"}]), "ASCII letters"),
        (_document(phrases=[{"id": "safe", "text": "  "}]), "must not be blank"),
        ({**_document(), "typo": 1}, "unknown field"),
        (_document(phrases=[{"id": "safe", "text": "One", "typo": 1}]), "unknown field"),
    ],
)
def test_invalid_scenario_is_rejected(document: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_scenario(document)


def test_record_duration_rounds_half_frame_up() -> None:
    scenario = parse_scenario(_document(record_seconds=1.5 / 16_000))

    assert scenario.target_frames == 2


def test_output_directory_must_exist_be_directory_and_empty(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert validate_empty_output_dir(empty) == empty.resolve()

    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="does not exist"):
        validate_empty_output_dir(missing)

    file_path = tmp_path / "file"
    file_path.write_text("data")
    with pytest.raises(ValueError, match="not a directory"):
        validate_empty_output_dir(file_path)

    (empty / ".hidden").write_text("data")
    with pytest.raises(ValueError, match="not empty"):
        validate_empty_output_dir(empty)


@pytest.mark.parametrize(("value", "expected"), [("1", 1), ("65535", 65_535)])
def test_port_accepts_boundaries(value: str, expected: int) -> None:
    assert _port(value) == expected


@pytest.mark.parametrize("value", ["0", "65536", "nope"])
def test_port_rejects_invalid_values(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _port(value)


def test_capture_keeps_exact_frame_prefix(tmp_path: Path) -> None:
    controller = DatasetController(logger=_logger(), scenario=_scenario("one"), output_dir=tmp_path)
    controller._set_current(0)
    controller._wait_delay(SessionState.PREPARING, 0.001)
    controller._begin_recording()

    controller.on_chunk(_chunk([1, 2, 3]))
    controller.on_chunk(_chunk([4, 5, 6]))
    samples = controller._wait_for_completed_buffer()

    assert samples.tolist() == [1, 2, 3, 4]
    assert controller.snapshot().state is SessionState.PUBLISHING


def test_capture_boundary_restarts_only_the_current_phrase(tmp_path: Path) -> None:
    controller = DatasetController(logger=_logger(), scenario=_scenario("one"), output_dir=tmp_path)
    controller._set_current(0)
    controller._wait_delay(SessionState.PREPARING, 0.001)
    controller._begin_recording()

    controller.on_chunk(_chunk([1, 2, 3]))
    controller.on_chunk(_chunk([10, 11], discontinuity=True))
    controller.on_chunk(_chunk([12, 13, 14]))
    samples = controller._wait_for_completed_buffer()

    assert samples.tolist() == [10, 11, 12, 13]
    assert controller.snapshot().state is SessionState.PUBLISHING


def test_completed_phrase_has_expected_wav_metadata(tmp_path: Path) -> None:
    controller = DatasetController(logger=_logger(), scenario=_scenario("one"), output_dir=tmp_path)
    samples = np.asarray([1, -2, 3, -4], dtype="<i2")

    controller._publish_wav(Phrase(id="one", text="One"), samples)

    with wave.open(str(tmp_path / "one.wav"), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16_000
        assert wav.getnframes() == 4
        assert wav.readframes(4) == samples.tobytes()


def test_cancellation_discards_current_phrase_and_keeps_completed_file(tmp_path: Path) -> None:
    recording_second = Event()
    controller: Any

    def produce() -> None:
        seen_index: int | None = None
        while True:
            snapshot = controller.snapshot()
            if snapshot.state in {SessionState.STOPPED, SessionState.ERROR, SessionState.COMPLETED}:
                return
            if snapshot.state is SessionState.RECORDING and snapshot.current_index != seen_index:
                seen_index = snapshot.current_index
                if seen_index == 0:
                    controller.on_chunk(_chunk([10, 11, 12, 13, 14]))
                else:
                    controller.on_chunk(_chunk([20, 21]))
                    recording_second.set()
            Event().wait(0.0005)

    fake = _FakePipeline(produce)

    def factory(handler: Any, device_id: str) -> Any:
        assert handler is controller
        assert device_id == "microphone-id"
        return fake

    controller = DatasetController(
        logger=_logger(),
        scenario=_scenario("first", "second"),
        output_dir=tmp_path,
        pipeline_factory=cast(Any, factory),
    )
    session = Thread(target=controller.run, args=("microphone-id",))
    session.start()
    assert recording_second.wait(timeout=1)

    controller.stop()
    session.join(timeout=1)

    assert not session.is_alive()
    assert controller.snapshot().state is SessionState.STOPPED
    assert (tmp_path / "first.wav").is_file()
    assert not (tmp_path / "second.wav").exists()
    assert not list(tmp_path.glob("*.tmp"))
    assert True in fake.immediate_stops
