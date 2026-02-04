"""Record a fixed-duration WAV for each phrase in a local test scenario."""

from __future__ import annotations

import os
import re
import json
import math
import wave
import asyncio
import argparse
import tempfile
from enum import Enum
from numbers import Real
from pathlib import Path
from threading import Lock, Event, Condition
from contextlib import suppress
from dataclasses import dataclass
from collections.abc import Mapping, Callable

import numpy as np
from nicegui import ui, app, run
from numpy.typing import NDArray
from lumivox_core.logger import Logger, LoggingConfig, get_logger, configure_logging

from lumivox_devicelab import (
    AudioFormat,
    CapturedChunk,
    PipelineError,
    CaptureContext,
    CaptureHandler,
    DeviceSnapshot,
    MicrophoneCapturePipeline,
    MicrophoneDeviceDiscovery,
)

_AUDIO_FORMAT = AudioFormat(sample_rate=16_000, channels=1)
_SCENARIO_FIELDS = frozenset({"start_delay_seconds", "phrase_delay_seconds", "record_seconds", "phrases"})
_PHRASE_FIELDS = frozenset({"id", "text"})
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")


@dataclass(frozen=True, slots=True)
class Phrase:
    id: str
    text: str


@dataclass(frozen=True, slots=True)
class Scenario:
    start_delay_seconds: float
    phrase_delay_seconds: float
    record_seconds: float
    phrases: tuple[Phrase, ...]
    target_frames: int


def _validate_fields(value: Mapping[object, object], expected: frozenset[str], *, name: str) -> None:
    actual = set(value)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise ValueError(f"{name} is missing field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{name} has unknown field(s): {', '.join(sorted(map(str, unknown)))}")


def _positive_seconds(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return result


def parse_scenario(value: object) -> Scenario:
    """Validate a decoded scenario document and preserve its phrase order."""
    if not isinstance(value, dict):
        raise ValueError("scenario must be a JSON object")
    _validate_fields(value, _SCENARIO_FIELDS, name="scenario")
    start_delay = _positive_seconds(value["start_delay_seconds"], name="start_delay_seconds")
    phrase_delay = _positive_seconds(value["phrase_delay_seconds"], name="phrase_delay_seconds")
    record_seconds = _positive_seconds(value["record_seconds"], name="record_seconds")

    raw_phrases = value["phrases"]
    if not isinstance(raw_phrases, list) or not raw_phrases:
        raise ValueError("phrases must be a non-empty array")
    phrases: list[Phrase] = []
    seen_ids: set[str] = set()
    for index, raw_phrase in enumerate(raw_phrases):
        name = f"phrases[{index}]"
        if not isinstance(raw_phrase, dict):
            raise ValueError(f"{name} must be an object")
        _validate_fields(raw_phrase, _PHRASE_FIELDS, name=name)
        phrase_id = raw_phrase["id"]
        text = raw_phrase["text"]
        if not isinstance(phrase_id, str):
            raise ValueError(f"{name}.id must be a string")
        if not isinstance(text, str):
            raise ValueError(f"{name}.text must be a string")
        phrase_id = phrase_id.strip()
        text = text.strip()
        if not phrase_id:
            raise ValueError(f"{name}.id must not be blank")
        if not text:
            raise ValueError(f"{name}.text must not be blank")
        if _SAFE_ID.fullmatch(phrase_id) is None:
            raise ValueError(f"{name}.id must contain only ASCII letters, digits, '_' or '-'")
        if phrase_id in seen_ids:
            raise ValueError(f"duplicate phrase id: {phrase_id}")
        seen_ids.add(phrase_id)
        phrases.append(Phrase(id=phrase_id, text=text))

    # Durations are rounded to the nearest frame, with exact half-frames rounded up.
    target_frames = math.floor(record_seconds * _AUDIO_FORMAT.sample_rate + 0.5)
    if target_frames < 1:
        raise ValueError("record_seconds is too short to produce one audio frame")
    return Scenario(
        start_delay_seconds=start_delay,
        phrase_delay_seconds=phrase_delay,
        record_seconds=record_seconds,
        phrases=tuple(phrases),
        target_frames=target_frames,
    )


def load_scenario(path: Path) -> Scenario:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except OSError as error:
        raise ValueError(f"cannot read scenario {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON in {path}: {error}") from error
    return parse_scenario(value)


def validate_empty_output_dir(path: Path) -> Path:
    output_dir = path.expanduser().resolve()
    if not output_dir.exists():
        raise ValueError(f"output directory does not exist: {output_dir}")
    if not output_dir.is_dir():
        raise ValueError(f"output path is not a directory: {output_dir}")
    try:
        if next(output_dir.iterdir(), None) is not None:
            raise ValueError(f"output directory is not empty: {output_dir}")
    except OSError as error:
        raise ValueError(f"cannot inspect output directory {output_dir}: {error}") from error
    return output_dir


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


class SessionState(str, Enum):
    READY = "ready"
    STARTING = "starting"
    START_DELAY = "start_delay"
    PREPARING = "preparing"
    RECORDING = "recording"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    state: SessionState
    current_index: int | None
    phrase: Phrase | None
    countdown: int | None
    completed_ids: tuple[str, ...]
    error: str | None


class _Cancelled(Exception):
    pass


PipelineFactory = Callable[[CaptureHandler, str], MicrophoneCapturePipeline]


class DatasetController(CaptureHandler):
    """Own one bounded recording session and its microphone pipeline."""

    def __init__(
        self,
        *,
        logger: Logger,
        scenario: Scenario,
        output_dir: Path,
        pipeline_factory: PipelineFactory | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        import time

        self._logger = logger.bind(module="devicelab")
        self.scenario = scenario
        self.output_dir = output_dir
        self._pipeline_factory = pipeline_factory or self._create_pipeline
        self._monotonic = monotonic or time.monotonic
        self._condition = Condition(Lock())
        self._cancelled = Event()
        self._interrupted = Event()
        self._state = SessionState.READY
        self._current_index: int | None = None
        self._deadline: float | None = None
        self._buffer: NDArray[np.int16] | None = None
        self._buffered_frames = 0
        self._recording_generation: int | None = None
        self._completed_buffer: NDArray[np.int16] | None = None
        self._completed_ids: list[str] = []
        self._error: str | None = None
        self._pipeline: MicrophoneCapturePipeline | None = None
        self._pipeline_stop_expected = False
        self._temporary_path: Path | None = None

    def _create_pipeline(self, handler: CaptureHandler, device_id: str) -> MicrophoneCapturePipeline:
        return MicrophoneCapturePipeline(
            logger=self._logger,
            handler=handler,
            audio_format=_AUDIO_FORMAT,
            device_id=device_id,
        )

    def discover(self) -> DeviceSnapshot:
        return MicrophoneDeviceDiscovery(logger=self._logger).snapshot()

    def snapshot(self) -> SessionSnapshot:
        with self._condition:
            deadline = self._deadline
            countdown = None if deadline is None else max(0, math.ceil(deadline - self._monotonic()))
            phrase = None if self._current_index is None else self.scenario.phrases[self._current_index]
            return SessionSnapshot(
                state=self._state,
                current_index=self._current_index,
                phrase=phrase,
                countdown=countdown,
                completed_ids=tuple(self._completed_ids),
                error=self._error,
            )

    def run(self, device_id: str) -> None:
        if not isinstance(device_id, str) or not device_id:
            self._fail(ValueError("select a microphone before starting"))
            return
        with self._condition:
            if self._state is not SessionState.READY:
                return
            self._state = SessionState.STARTING
        try:
            validate_empty_output_dir(self.output_dir)
            pipeline = self._pipeline_factory(self, device_id)
            with self._condition:
                if self._cancelled.is_set():
                    raise _Cancelled
                self._pipeline = pipeline
            if self._cancelled.is_set():
                raise _Cancelled
            pipeline.start()
            self._wait_delay(SessionState.START_DELAY, self.scenario.start_delay_seconds)
            for index, phrase in enumerate(self.scenario.phrases):
                self._set_current(index)
                self._wait_delay(SessionState.PREPARING, self.scenario.phrase_delay_seconds)
                self._begin_recording()
                samples = self._wait_for_completed_buffer()
                self._publish_wav(phrase, samples)
            with self._condition:
                self._raise_if_interrupted_locked()
                self._pipeline_stop_expected = True
            pipeline.stop()
            with self._condition:
                self._raise_if_interrupted_locked()
                self._pipeline = None
                self._state = SessionState.COMPLETED
                self._deadline = None
                self._condition.notify_all()
        except _Cancelled:
            self._finish_stopped()
        except Exception as error:
            if self._cancelled.is_set():
                self._finish_stopped()
            else:
                self._fail(error)
        finally:
            with self._condition:
                current_pipeline = self._pipeline
                terminal = self._state in {SessionState.COMPLETED, SessionState.STOPPED, SessionState.ERROR}
            if current_pipeline is not None and terminal:
                with suppress(Exception):
                    current_pipeline.stop(immediate=self._state is not SessionState.COMPLETED)
                with self._condition:
                    if self._pipeline is current_pipeline:
                        self._pipeline = None

    def stop(self) -> None:
        self.request_stop()
        with self._condition:
            pipeline = self._pipeline
        if pipeline is not None:
            with suppress(Exception):
                pipeline.stop(immediate=True)

    def request_stop(self) -> None:
        """Signal cancellation without blocking on pipeline teardown."""
        with self._condition:
            self._cancelled.set()
            self._interrupted.set()
            if self._state not in {
                SessionState.READY,
                SessionState.COMPLETED,
                SessionState.STOPPED,
                SessionState.ERROR,
            }:
                self._state = SessionState.STOPPED
                self._deadline = None
                self._discard_current_locked()
            self._condition.notify_all()

    def shutdown(self) -> None:
        self.stop()
        with self._condition:
            temporary_path = self._temporary_path
            self._temporary_path = None
        if temporary_path is not None:
            with suppress(FileNotFoundError):
                temporary_path.unlink()

    def on_chunk(self, chunk: CapturedChunk) -> None:
        boundary = False
        phrase_id: str | None = None
        with self._condition:
            if self._state is not SessionState.RECORDING or self._buffer is None:
                return
            if self._recording_generation is None:
                self._recording_generation = chunk.generation
            elif chunk.discontinuity or chunk.generation != self._recording_generation:
                boundary = True
                if self._current_index is not None:
                    phrase_id = self.scenario.phrases[self._current_index].id
                self._reset_current_recording_locked()
                self._recording_generation = chunk.generation
            remaining = self.scenario.target_frames - self._buffered_frames
            accepted = min(remaining, len(chunk.samples))
            assert self._buffer is not None
            self._buffer[self._buffered_frames : self._buffered_frames + accepted] = chunk.samples[:accepted]
            self._buffered_frames += accepted
            if self._buffered_frames == self.scenario.target_frames:
                self._completed_buffer = self._buffer
                self._buffer = None
                self._state = SessionState.PUBLISHING
                self._deadline = None
                self._condition.notify_all()
        if boundary:
            self._logger.warning(
                "dataset_phrase_restarted_after_capture_boundary",
                generation=chunk.generation,
                phrase_id=phrase_id,
            )

    def on_restart(self, context: CaptureContext, cause: PipelineError) -> None:
        restarted = False
        with self._condition:
            if self._state is SessionState.RECORDING:
                restarted = True
                self._reset_current_recording_locked()
                self._condition.notify_all()
        if restarted:
            self._logger.warning(
                "dataset_phrase_restarted_after_microphone_recovery",
                generation=context.generation,
                error=str(cause),
            )

    def on_stop(self, context: CaptureContext, cause: PipelineError | None) -> None:
        with self._condition:
            active = self._state not in {SessionState.COMPLETED, SessionState.STOPPED, SessionState.ERROR}
            if active and not self._pipeline_stop_expected and not self._cancelled.is_set():
                self._error = str(cause) if cause is not None else "microphone pipeline stopped unexpectedly"
                self._state = SessionState.ERROR
                self._interrupted.set()
                self._discard_current_locked()
                self._condition.notify_all()

    def _wait_delay(self, state: SessionState, duration: float) -> None:
        with self._condition:
            self._raise_if_interrupted_locked()
            self._interrupted.clear()
            self._state = state
            self._deadline = self._monotonic() + duration
            self._condition.notify_all()
        self._interrupted.wait(duration)
        with self._condition:
            self._raise_if_interrupted_locked()
            self._deadline = None

    def _set_current(self, index: int) -> None:
        with self._condition:
            self._raise_if_interrupted_locked()
            self._current_index = index
            self._condition.notify_all()

    def _begin_recording(self) -> None:
        with self._condition:
            self._raise_if_interrupted_locked()
            if self._state is not SessionState.PREPARING:
                raise RuntimeError(f"cannot begin recording from state {self._state.value}")
            self._buffer = np.empty(self.scenario.target_frames, dtype="<i2")
            self._buffered_frames = 0
            self._recording_generation = None
            self._completed_buffer = None
            self._state = SessionState.RECORDING
            self._deadline = self._monotonic() + self.scenario.record_seconds
            self._condition.notify_all()

    def _wait_for_completed_buffer(self) -> NDArray[np.int16]:
        with self._condition:
            self._condition.wait_for(
                lambda: self._completed_buffer is not None
                or self._cancelled.is_set()
                or self._state is SessionState.ERROR
            )
            if self._cancelled.is_set():
                raise _Cancelled
            if self._state is SessionState.ERROR:
                raise RuntimeError(self._error or "dataset session failed")
            assert self._completed_buffer is not None
            samples = self._completed_buffer
            self._completed_buffer = None
            return samples

    def _publish_wav(self, phrase: Phrase, samples: NDArray[np.int16]) -> None:
        final_path = self.output_dir / f"{phrase.id}.wav"
        if final_path.exists():
            raise FileExistsError(f"output file appeared during the session: {final_path}")
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{phrase.id}.", suffix=".tmp", dir=self.output_dir)
        temporary_path = Path(temporary_name)
        with self._condition:
            self._temporary_path = temporary_path
        try:
            with os.fdopen(descriptor, "w+b") as stream:
                with wave.open(stream, "wb") as wav:
                    wav.setnchannels(_AUDIO_FORMAT.channels)
                    wav.setsampwidth(2)
                    wav.setframerate(_AUDIO_FORMAT.sample_rate)
                    wav.writeframes(samples.tobytes())
                stream.flush()
                os.fsync(stream.fileno())
            with self._condition:
                if self._cancelled.is_set():
                    raise _Cancelled
                os.link(temporary_path, final_path)
                self._completed_ids.append(phrase.id)
                self._condition.notify_all()
            with suppress(FileNotFoundError):
                temporary_path.unlink()
            directory_fd = os.open(self.output_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            with suppress(FileNotFoundError):
                temporary_path.unlink()
            with self._condition:
                if self._temporary_path == temporary_path:
                    self._temporary_path = None

    def _discard_current_locked(self) -> None:
        self._buffer = None
        self._completed_buffer = None
        self._buffered_frames = 0
        self._recording_generation = None

    def _reset_current_recording_locked(self) -> None:
        self._buffer = np.empty(self.scenario.target_frames, dtype="<i2")
        self._buffered_frames = 0
        self._recording_generation = None
        self._deadline = self._monotonic() + self.scenario.record_seconds

    def _raise_if_interrupted_locked(self) -> None:
        if self._cancelled.is_set():
            raise _Cancelled
        if self._state is SessionState.ERROR:
            raise RuntimeError(self._error or "dataset session failed")

    def _finish_stopped(self) -> None:
        with self._condition:
            self._state = SessionState.STOPPED
            self._deadline = None
            self._discard_current_locked()
            self._condition.notify_all()

    def _fail(self, error: BaseException) -> None:
        with self._condition:
            self._error = str(error)
            self._state = SessionState.ERROR
            self._interrupted.set()
            self._deadline = None
            self._discard_current_locked()
            self._condition.notify_all()
        self._logger.error("dataset_session_failed", error=str(error))


def _device_options(snapshot: DeviceSnapshot) -> dict[str, str]:
    return {device.id: f"{device.name}{' (default)' if device.is_default else ''}" for device in snapshot.devices}


def _preferred_device(snapshot: DeviceSnapshot, current: object) -> str | None:
    if isinstance(current, str) and any(device.id == current for device in snapshot.devices):
        return current
    if snapshot.default is not None:
        return snapshot.default.id
    return snapshot.devices[0].id if snapshot.devices else None


def build_ui(controller: DatasetController) -> None:
    ui.colors(primary="#22577a", secondary="#16324f", positive="#2d6a4f", negative="#b42318")
    ui.add_css(
        """
        body { margin: 0; background: #eef2f3; color: #102a43; font-family: Inter, sans-serif; }
        .q-loading-bar { display: none !important; }
        .recorder-shell { min-height: 100vh; width: min(1100px, 100%); margin: auto; padding: 24px; }
        .state-panel { min-height: 52vh; transition: background-color .2s; }
        .phrase { font-size: clamp(2.4rem, 7vw, 6.5rem); line-height: 1.06; overflow-wrap: anywhere; }
        .countdown { font-size: clamp(5rem, 20vw, 15rem); line-height: .9; font-variant-numeric: tabular-nums; }
        """
    )
    with ui.column().classes("recorder-shell gap-5 justify-between"):
        with ui.row().classes("w-full items-end gap-3 flex-wrap"):
            microphone_select = ui.select({}, label="Microphone", with_input=True).classes("grow min-w-64")
            refresh_button = ui.button("Refresh devices", icon="refresh").props("outline")
            start_button = ui.button("Start", icon="play_arrow")
            stop_button = ui.button("Stop", icon="stop", color="negative")
        ui.label(
            f"{len(controller.scenario.phrases)} phrases | initial {controller.scenario.start_delay_seconds:g}s | "
            f"prepare {controller.scenario.phrase_delay_seconds:g}s | record {controller.scenario.record_seconds:g}s"
        ).classes("text-sm font-medium")
        ui.label(str(controller.output_dir)).classes("text-xs font-mono break-all text-slate-600")
        with ui.column().classes(
            "state-panel w-full items-center justify-center text-center p-6 sm:p-10 rounded-lg"
        ) as panel:
            position_label = ui.label("Ready").classes("text-xl sm:text-2xl font-bold uppercase tracking-widest")
            phrase_label = ui.label("Select a microphone to begin").classes("phrase font-black")
            countdown_label = ui.label("").classes("countdown font-black")
            detail_label = ui.label("").classes("text-lg sm:text-2xl font-semibold whitespace-pre-wrap")
    session_started = False

    async def refresh_devices() -> None:
        refresh_button.disable()
        detail_label.set_text("Discovering PipeWire microphones...")
        try:
            snapshot = await run.io_bound(controller.discover)
            if snapshot is None:
                return
            if controller.snapshot().state is not SessionState.READY:
                return
            microphone_select.options = _device_options(snapshot)
            microphone_select.value = _preferred_device(snapshot, microphone_select.value)
            microphone_select.update()
            start_button.set_enabled(bool(snapshot.devices))
            detail_label.set_text(
                f"Ready: {len(snapshot.devices)} microphone(s)" if snapshot.devices else "No microphones discovered"
            )
        except Exception as error:
            detail_label.set_text(f"Device discovery failed: {error}")
            ui.notify(str(error), type="negative", close_button=True)
        except asyncio.CancelledError:
            return
        finally:
            refresh_button.set_enabled(controller.snapshot().state is SessionState.READY)

    async def start_session() -> None:
        nonlocal session_started
        device_id = microphone_select.value
        if not isinstance(device_id, str):
            ui.notify("Select a microphone", type="warning")
            return
        try:
            validate_empty_output_dir(controller.output_dir)
        except ValueError as error:
            ui.notify(str(error), type="negative", close_button=True)
            return
        microphone_select.disable()
        refresh_button.disable()
        start_button.disable()
        stop_button.enable()
        session_started = True
        try:
            await run.io_bound(controller.run, device_id)
        except asyncio.CancelledError:
            controller.request_stop()

    async def stop_or_exit() -> None:
        try:
            snapshot = controller.snapshot()
            if snapshot.state in {SessionState.COMPLETED, SessionState.STOPPED, SessionState.ERROR}:
                await run.io_bound(controller.shutdown)
                app.shutdown()
            else:
                stop_button.disable()
                await run.io_bound(controller.stop)
        except asyncio.CancelledError:
            controller.request_stop()

    async def stop_on_disconnect() -> None:
        try:
            if session_started and controller.snapshot().state not in {
                SessionState.READY,
                SessionState.COMPLETED,
                SessionState.STOPPED,
                SessionState.ERROR,
            }:
                await run.io_bound(controller.stop)
        except asyncio.CancelledError:
            controller.request_stop()

    def refresh_display() -> None:
        snapshot = controller.snapshot()
        state = snapshot.state
        colors = {
            SessionState.READY: ("#f8fafc", "#102a43"),
            SessionState.STARTING: ("#fff3bf", "#3d2c00"),
            SessionState.START_DELAY: ("#ffd166", "#2d2500"),
            SessionState.PREPARING: ("#8ecae6", "#082f49"),
            SessionState.RECORDING: ("#b42318", "#ffffff"),
            SessionState.PUBLISHING: ("#f4a261", "#3d1f00"),
            SessionState.COMPLETED: ("#2d6a4f", "#ffffff"),
            SessionState.STOPPED: ("#64748b", "#ffffff"),
            SessionState.ERROR: ("#7f1d1d", "#ffffff"),
        }
        background, foreground = colors[state]
        panel.style(replace=f"background: {background}; color: {foreground}")
        total = len(controller.scenario.phrases)
        position = "" if snapshot.current_index is None else f"{snapshot.current_index + 1} / {total}"
        phrase_text = snapshot.phrase.text if snapshot.phrase is not None else ""
        countdown_label.set_text("" if snapshot.countdown is None else str(snapshot.countdown))
        if state is SessionState.READY:
            position_label.set_text("Ready")
            phrase_label.set_text("Select a microphone to begin")
        elif state is SessionState.STARTING:
            position_label.set_text("Starting microphone")
            phrase_label.set_text("Please wait")
        elif state is SessionState.START_DELAY:
            position_label.set_text("Get ready")
            phrase_label.set_text("Move away from the computer")
        elif state is SessionState.PREPARING:
            position_label.set_text(position)
            phrase_label.set_text(phrase_text)
            detail_label.set_text("Prepare to speak")
        elif state is SessionState.RECORDING:
            position_label.set_text(f"REC | {position}")
            phrase_label.set_text(phrase_text)
            detail_label.set_text("Recording now")
        elif state is SessionState.PUBLISHING:
            position_label.set_text(position)
            phrase_label.set_text(phrase_text)
            detail_label.set_text("Saving WAV...")
        elif state is SessionState.COMPLETED:
            position_label.set_text("Completed")
            phrase_label.set_text(f"{len(snapshot.completed_ids)} / {total} phrases saved")
            detail_label.set_text(
                f"{controller.output_dir}\n" + "\n".join(f"{item}.wav" for item in snapshot.completed_ids)
            )
        elif state is SessionState.STOPPED:
            position_label.set_text("Stopped")
            phrase_label.set_text(f"Retained {len(snapshot.completed_ids)} / {total} files")
            detail_label.set_text(str(controller.output_dir))
        else:
            position_label.set_text("Error")
            phrase_label.set_text(f"Retained {len(snapshot.completed_ids)} / {total} files")
            detail_label.set_text(snapshot.error or "Unknown session error")
        terminal = state in {SessionState.COMPLETED, SessionState.STOPPED, SessionState.ERROR}
        stop_button.set_text("Exit" if terminal else "Stop")
        stop_button.set_enabled(state is not SessionState.READY or terminal)
        if terminal:
            refresh_timer.cancel()

    refresh_button.on_click(refresh_devices)
    start_button.on_click(start_session)
    stop_button.on_click(stop_or_exit)
    start_button.disable()
    stop_button.disable()
    ui.context.client.on_disconnect(stop_on_disconnect)
    refresh_timer = ui.timer(0.1, refresh_display)
    ui.timer(0.0, refresh_devices, once=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record fixed-duration test phrases from a PipeWire microphone")
    parser.add_argument("--config", required=True, type=Path, help="scenario JSON path")
    parser.add_argument("--output-dir", required=True, type=Path, help="existing empty output directory")
    parser.add_argument("--port", type=_port, default=8080, help="local HTTP port (default: 8080)")
    args = parser.parse_args()
    try:
        args.config = args.config.expanduser().resolve()
        args.scenario = load_scenario(args.config)
        args.output_dir = validate_empty_output_dir(args.output_dir)
    except ValueError as error:
        parser.error(str(error))
    return args


def main() -> None:
    args = _parse_args()
    configure_logging(LoggingConfig(application="devicelab-record-test-dataset"))
    controller = DatasetController(
        logger=get_logger(tool="record_test_dataset"),
        scenario=args.scenario,
        output_dir=args.output_dir,
    )
    app.config.socket_io_js_transports = ["polling"]
    app.on_shutdown(controller.shutdown)

    @ui.page("/")
    def index() -> None:
        build_ui(controller)

    try:
        ui.run(host="127.0.0.1", port=args.port, title="Devicelab Test Dataset Recorder", reload=False)
    except KeyboardInterrupt:
        pass
    finally:
        controller.shutdown()


if __name__ in {"__main__", "__mp_main__"}:
    main()
