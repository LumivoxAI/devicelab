"""Record a microphone and play the resulting WAV through selected devices."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from threading import Lock, Event
from contextlib import suppress

from nicegui import ui, app, run
from lumivox_core.logger import Logger, LoggingConfig, get_logger, configure_logging

from lumivox_devicelab import (
    AudioFormat,
    CapturedChunk,
    CaptureHandler,
    DeviceSnapshot,
    FileReplayMode,
    FileCapturePipeline,
    SpeakerDeviceDiscovery,
    SpeakerPlaybackPipeline,
    MicrophoneCapturePipeline,
    MicrophoneDeviceDiscovery,
)

_AUDIO_FORMAT = AudioFormat(sample_rate=16_000, channels=1)


class _FrameCounter(CaptureHandler):
    def __init__(self) -> None:
        self._frames = 0
        self._lock = Lock()

    @property
    def frames(self) -> int:
        with self._lock:
            return self._frames

    def on_chunk(self, chunk: CapturedChunk) -> None:
        with self._lock:
            self._frames += len(chunk.samples)


class _PlaybackHandler(CaptureHandler):
    def __init__(self, speaker: SpeakerPlaybackPipeline) -> None:
        self._speaker = speaker

    def on_chunk(self, chunk: CapturedChunk) -> None:
        self._speaker.submit(chunk.samples)


class _DemoController:
    """Own the single audio session shared by the local demo UI."""

    def __init__(self, *, logger: Logger, output_path: Path) -> None:
        self._logger = logger
        self.output_path = output_path
        self._lock = Lock()
        self._playback_cancelled = Event()
        self._recording: MicrophoneCapturePipeline | None = None
        self._counter: _FrameCounter | None = None
        self._playback_reserved = False
        self._file_capture: FileCapturePipeline | None = None
        self._speaker: SpeakerPlaybackPipeline | None = None

    @property
    def recorded_frames(self) -> int:
        with self._lock:
            counter = self._counter
        return 0 if counter is None else counter.frames

    @property
    def has_recording(self) -> bool:
        return self.output_path.is_file()

    def discover(self) -> tuple[DeviceSnapshot, DeviceSnapshot]:
        microphones = MicrophoneDeviceDiscovery(logger=self._logger).snapshot()
        speakers = SpeakerDeviceDiscovery(logger=self._logger).snapshot()
        return microphones, speakers

    def start_recording(self, device_id: str) -> None:
        counter = _FrameCounter()
        pipeline = MicrophoneCapturePipeline(
            logger=self._logger,
            handler=counter,
            audio_format=_AUDIO_FORMAT,
            device_id=device_id,
            record_to=self.output_path,
            overwrite=True,
        )
        with self._lock:
            self._ensure_idle()
            self._recording = pipeline
            self._counter = counter
        try:
            pipeline.start()
        except Exception:
            with suppress(Exception):
                pipeline.stop(immediate=True)
            with self._lock:
                if self._recording is pipeline:
                    self._recording = None
                    self._counter = None
            raise

    def stop_recording(self) -> None:
        with self._lock:
            pipeline = self._recording
        if pipeline is None:
            return
        try:
            pipeline.stop()
        finally:
            with self._lock:
                if self._recording is pipeline:
                    self._recording = None

    def prepare_playback(self) -> None:
        if not self.output_path.is_file():
            raise FileNotFoundError(f"recording does not exist: {self.output_path}")
        with self._lock:
            self._ensure_idle()
            self._playback_cancelled.clear()
            self._playback_reserved = True

    def play(self, device_id: str) -> bool:
        if not self.output_path.is_file():
            raise FileNotFoundError(f"recording does not exist: {self.output_path}")
        speaker: SpeakerPlaybackPipeline | None = None
        file_capture: FileCapturePipeline | None = None
        failure: Exception | None = None
        try:
            with self._lock:
                if not self._playback_reserved:
                    self._ensure_idle()
                    self._playback_cancelled.clear()
                    self._playback_reserved = True
            speaker = SpeakerPlaybackPipeline(
                logger=self._logger,
                audio_format=_AUDIO_FORMAT,
                device_id=device_id,
            )
            with self._lock:
                self._speaker = speaker
            if self._playback_cancelled.is_set():
                return False
            speaker.start()
            if self._playback_cancelled.is_set():
                return False
            file_capture = FileCapturePipeline(
                logger=self._logger,
                handler=_PlaybackHandler(speaker),
                audio_format=_AUDIO_FORMAT,
                path=self.output_path,
                replay_mode=FileReplayMode.AS_FAST_AS_POSSIBLE,
            )
            with self._lock:
                self._file_capture = file_capture
            if self._playback_cancelled.is_set():
                return False
            file_capture.start()
            file_capture.wait()
        except Exception as error:
            failure = error
        finally:
            cancelled = self._playback_cancelled.is_set()
            if file_capture is not None:
                with suppress(Exception):
                    file_capture.stop(immediate=cancelled or failure is not None)
            if speaker is not None:
                try:
                    speaker.stop(immediate=cancelled or failure is not None)
                except Exception as error:
                    if failure is None:
                        failure = error
            with self._lock:
                if self._file_capture is file_capture:
                    self._file_capture = None
                if self._speaker is speaker:
                    self._speaker = None
                self._playback_reserved = False

        if failure is not None and not self._playback_cancelled.is_set():
            raise failure
        return not self._playback_cancelled.is_set()

    def stop_playback(self) -> None:
        self._playback_cancelled.set()
        with self._lock:
            speaker = self._speaker
            file_capture = self._file_capture
        if speaker is not None:
            with suppress(Exception):
                speaker.stop(immediate=True)
        if file_capture is not None:
            with suppress(Exception):
                file_capture.stop(immediate=True)

    def shutdown(self) -> None:
        self.stop_playback()
        with self._lock:
            recording = self._recording
        if recording is not None:
            with suppress(Exception):
                recording.stop(immediate=True, timeout=3.0)

    def _ensure_idle(self) -> None:
        if (
            self._recording is not None
            or self._playback_reserved
            or self._speaker is not None
            or self._file_capture is not None
        ):
            raise RuntimeError("another audio operation is already active")


def _device_options(snapshot: DeviceSnapshot) -> dict[str, str]:
    return {device.id: f"{device.name}{' (default)' if device.is_default else ''}" for device in snapshot.devices}


def _preferred_device(snapshot: DeviceSnapshot, current: object) -> str | None:
    if isinstance(current, str) and any(device.id == current for device in snapshot.devices):
        return current
    if snapshot.default is not None:
        return snapshot.default.id
    return snapshot.devices[0].id if snapshot.devices else None


def build_ui(controller: _DemoController) -> None:
    ui.colors(primary="#ff6b35", secondary="#162a3a", accent="#f7c548", dark="#0d1b26")
    ui.add_css(
        """
        body { background: #f4efe6; color: #162a3a; }
        .q-loading-bar { display: none !important; }
        .demo-shell { width: min(920px, calc(100vw - 32px)); margin: 48px auto; }
        .demo-card { background: #fffaf2; border: 1px solid #d9cdbc; box-shadow: 8px 8px 0 #162a3a; }
        .status-strip { border-left: 5px solid #ff6b35; background: #f1e7d7; }
        """
    )

    with ui.column().classes("demo-shell gap-7"):
        ui.label("DEVICE LAB / LOOPBACK").classes("text-xs font-bold tracking-[0.28em] text-orange-8")
        ui.label("Record here. Play there.").classes("text-4xl sm:text-6xl font-black leading-none")
        ui.label("A local microphone-to-WAV-to-speaker pipeline check.").classes("text-lg text-slate-600")

        with ui.card().classes("demo-card w-full p-6 gap-6 rounded-none"):
            with ui.row().classes("w-full items-end gap-4"):
                microphone_select = ui.select({}, label="Microphone", with_input=True).classes("grow min-w-64")
                speaker_select = ui.select({}, label="Speaker", with_input=True).classes("grow min-w-64")
                refresh_button = ui.button("Refresh", icon="refresh").props("outline")

            with ui.row().classes("w-full gap-3 flex-wrap"):
                record_button = ui.button("Record", icon="fiber_manual_record")
                stop_record_button = ui.button("Stop recording", icon="stop", color="secondary")
                play_button = ui.button("Play", icon="play_arrow", color="secondary")
                stop_play_button = ui.button("Stop playback", icon="stop", color="negative")

            with ui.column().classes("status-strip w-full p-4 gap-1"):
                status_label = ui.label("Loading devices...").classes("text-lg font-bold")
                detail_label = ui.label(str(controller.output_path)).classes(
                    "text-sm font-mono break-all text-slate-600"
                )
                meter_label = ui.label("16 kHz / mono / PCM S16LE").classes("text-xs uppercase tracking-wider")

    def set_idle_controls() -> None:
        has_microphone = bool(microphone_select.options)
        has_speaker = bool(speaker_select.options)
        microphone_select.enable()
        speaker_select.enable()
        refresh_button.enable()
        record_button.set_enabled(has_microphone)
        stop_record_button.disable()
        play_button.set_enabled(has_speaker and controller.has_recording)
        stop_play_button.disable()

    def show_error(action: str, error: BaseException) -> None:
        status_label.set_text(f"{action} failed")
        detail_label.set_text(str(error))
        ui.notify(str(error), type="negative", close_button=True)
        set_idle_controls()

    async def refresh_devices() -> None:
        refresh_button.disable()
        status_label.set_text("Discovering PipeWire devices...")
        try:
            result = await run.io_bound(controller.discover)
            if result is None:
                return
            microphones, speakers = result
            microphone_select.options = _device_options(microphones)
            speaker_select.options = _device_options(speakers)
            microphone_select.value = _preferred_device(microphones, microphone_select.value)
            speaker_select.value = _preferred_device(speakers, speaker_select.value)
            microphone_select.update()
            speaker_select.update()
            status_label.set_text(
                f"Ready: {len(microphones.devices)} microphone(s), {len(speakers.devices)} speaker(s)"
                if microphones.devices or speakers.devices
                else "No PipeWire audio devices discovered"
            )
            detail_label.set_text(str(controller.output_path))
            set_idle_controls()
        except Exception as error:
            show_error("Device discovery", error)

    async def start_recording() -> None:
        device_id = microphone_select.value
        if not isinstance(device_id, str):
            return
        microphone_select.disable()
        speaker_select.disable()
        refresh_button.disable()
        record_button.disable()
        play_button.disable()
        stop_play_button.disable()
        status_label.set_text("Starting microphone...")
        detail_label.set_text(str(controller.output_path))
        try:
            await run.io_bound(controller.start_recording, device_id)
            status_label.set_text("Recording")
            stop_record_button.enable()
        except Exception as error:
            show_error("Recording", error)

    async def stop_recording() -> None:
        stop_record_button.disable()
        status_label.set_text("Finalizing WAV...")
        try:
            await run.io_bound(controller.stop_recording)
            status_label.set_text("Recording ready")
            detail_label.set_text(str(controller.output_path))
            set_idle_controls()
        except Exception as error:
            show_error("Stopping recording", error)

    async def play_recording() -> None:
        device_id = speaker_select.value
        if not isinstance(device_id, str):
            return
        try:
            controller.prepare_playback()
        except Exception as error:
            show_error("Playback", error)
            return
        microphone_select.disable()
        speaker_select.disable()
        refresh_button.disable()
        record_button.disable()
        stop_record_button.disable()
        play_button.disable()
        stop_play_button.enable()
        status_label.set_text("Playing through the selected speaker")
        try:
            completed = await run.io_bound(controller.play, device_id)
            status_label.set_text("Playback finished" if completed else "Playback stopped")
            detail_label.set_text(str(controller.output_path))
            set_idle_controls()
        except Exception as error:
            show_error("Playback", error)

    async def stop_playback() -> None:
        stop_play_button.disable()
        status_label.set_text("Stopping playback...")
        await run.io_bound(controller.stop_playback)

    def update_meter() -> None:
        frames = controller.recorded_frames
        if frames:
            meter_label.set_text(f"{frames / _AUDIO_FORMAT.sample_rate:.1f} s captured / 16 kHz mono")

    refresh_button.on_click(refresh_devices)
    record_button.on_click(start_recording)
    stop_record_button.on_click(stop_recording)
    play_button.on_click(play_recording)
    stop_play_button.on_click(stop_playback)
    stop_record_button.disable()
    stop_play_button.disable()
    record_button.disable()
    play_button.disable()
    ui.timer(0.25, update_meter)
    ui.timer(0.0, refresh_devices, once=True)


def _parse_args() -> argparse.Namespace:
    default_output = Path(tempfile.gettempdir()) / "lumivox-devicelab-demo.wav"
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=default_output, help="WAV output path")
    parser.add_argument("--port", type=_port, default=8080, help="local HTTP port (default: 8080)")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if output.suffix.lower() != ".wav":
        parser.error("--output must have a .wav extension")
    if not output.parent.is_dir():
        parser.error(f"output directory does not exist: {output.parent}")
    args.output = output
    return args


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main() -> None:
    args = _parse_args()
    configure_logging(LoggingConfig(application="devicelab-record-playback-demo"))
    controller = _DemoController(logger=get_logger(example="record_playback_gui"), output_path=args.output)
    app.config.socket_io_js_transports = ["polling"]
    app.on_shutdown(controller.shutdown)

    @ui.page("/")
    def index() -> None:
        build_ui(controller)

    try:
        ui.run(host="127.0.0.1", port=args.port, title="Devicelab Loopback", reload=False)
    except KeyboardInterrupt:
        pass
    finally:
        controller.shutdown()


if __name__ in {"__main__", "__mp_main__"}:
    main()
