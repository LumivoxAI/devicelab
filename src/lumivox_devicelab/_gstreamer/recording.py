"""Reusable bounded WAV/FLAC recording branch."""

from __future__ import annotations

import os
import time
import tempfile
from os import PathLike
from pathlib import Path
from threading import Lock

from lumivox_core.logger import Logger

from lumivox_devicelab.errors import PipelineTimeoutError

from .graph import _PipelineGraph
from .runtime import GStreamerElementError, get_gst
from .elements.file import WavEnc, FlacEnc, FileSink
from .elements.flow import Tee, AudioQueue, QueueOverflowPolicy

_RECORDING_QUEUE_TIME_MS = 1_000
_SUPPORTED_SUFFIXES = frozenset((".wav", ".flac"))


def validate_recording_path(path: str | PathLike[str], *, overwrite: bool) -> Path:
    """Validate recording configuration without creating or truncating a file."""
    if not isinstance(overwrite, bool):
        raise TypeError("overwrite must be a bool")
    value = Path(os.fspath(path))
    if not value.name:
        raise ValueError("recording path must include a file name")
    if value.suffix.lower() not in _SUPPORTED_SUFFIXES:
        raise ValueError("recording path must have a .wav or .flac extension")
    if not value.parent.is_dir():
        raise ValueError("recording parent directory must exist")
    if value.exists() and not overwrite:
        raise ValueError(f"recording path already exists: {value}")
    return value


def recording_segment_path(path: Path, generation: int) -> Path:
    if generation < 0:
        raise ValueError("generation must be non-negative")
    if generation == 0:
        return path
    return path.with_name(f"{path.stem}.{generation}{path.suffix}")


class _RecordingError(GStreamerElementError):
    """A recording branch failure that microphone recovery must not retry."""


class _RecordingBranch:
    """Own one generation's recording elements and EOS completion."""

    def __init__(
        self,
        *,
        logger: Logger,
        path: Path,
        temporary_path: Path,
        queue: AudioQueue,
        encoder: WavEnc | FlacEnc,
        sink: FileSink,
        bus: object,
        overwrite: bool,
    ) -> None:
        self._logger = logger.bind(module="devicelab")
        self.path = path
        self._temporary_path = temporary_path
        self._queue = queue
        self._sink = sink
        self._error_sources = (queue.impl, encoder.impl, sink.impl)
        self._bus = bus
        self._overwrite = overwrite
        self._active = False
        self._finalized = False
        self._completion_lock = Lock()

    @classmethod
    def build(
        cls,
        *,
        graph: _PipelineGraph,
        tee: Tee,
        logger: Logger,
        path: Path,
        overwrite: bool,
    ) -> _RecordingBranch:
        temporary_path = _prepare_temporary_path(path, overwrite=overwrite)
        try:
            queue = AudioQueue(
                max_time_ms=_RECORDING_QUEUE_TIME_MS,
                overflow_policy=QueueOverflowPolicy.BLOCK,
                name="recording-queue",
            )
            encoder = (
                WavEnc(name="recording-encoder") if path.suffix.lower() == ".wav" else FlacEnc(name="recording-encoder")
            )
            sink = FileSink(temporary_path, name="recording-sink")
            graph.add(queue, encoder, sink)
            graph.branch(tee, queue, encoder, sink)
            graph.use(lambda pipeline: pipeline.set_property("message-forward", True))
            bus = graph.use(lambda pipeline: pipeline.get_bus())
            if bus is None:
                raise GStreamerElementError("recording pipeline has no bus")
        except Exception as error:
            temporary_path.unlink(missing_ok=True)
            raise _RecordingError(f"failed to build recording branch for {path}") from error
        return cls(
            logger=logger,
            path=path,
            temporary_path=temporary_path,
            queue=queue,
            encoder=encoder,
            sink=sink,
            bus=bus,
            overwrite=overwrite,
        )

    def mark_active(self) -> None:
        self._active = True

    def finalize(self, deadline: float) -> None:
        """Finalize and atomically publish this branch before graph NULL."""
        if self._finalized:
            return
        if not self._active:
            self._discard_unstarted()
            self._finalized = True
            return

        self._logger.debug("recording_finalization_started", path=str(self.path))
        self._send_eos()
        self._wait_for_sink_eos(deadline)
        self._publish()
        self._finalized = True
        self._logger.info("recording_finalized", path=str(self.path))

    def complete_source_eos(self, deadline: float) -> None:
        """Publish after EOS has already entered the branch from its source."""
        del deadline
        with self._completion_lock:
            if self._finalized:
                return
            if not self._active:
                self._discard_unstarted()
                self._finalized = True
                return
            # Top-level pipeline EOS is emitted only after every sink, including
            # this encoder branch, has completed EOS processing.
            self._logger.debug("recording_finalization_started", path=str(self.path))
            self._publish()
            self._finalized = True
            self._logger.info("recording_finalized", path=str(self.path))

    def abort(self) -> None:
        """Remove an unpublished temporary recording after abortive shutdown."""
        with self._completion_lock:
            if self._finalized:
                return
            self._temporary_path.unlink(missing_ok=True)
            self._finalized = True
            self._logger.warning(
                "recording_aborted",
                path=str(self.path),
                temporary_path=str(self._temporary_path),
            )

    def _discard_unstarted(self) -> None:
        self._temporary_path.unlink(missing_ok=True)
        self._logger.debug("unstarted_recording_removed", path=str(self.path))

    def _send_eos(self) -> None:
        gst = get_gst()
        try:
            accepted = self._queue.impl.send_event(gst.Event.new_eos())
        except Exception as error:
            raise _RecordingError(f"failed to send EOS to recording branch for {self.path}") from error
        if not accepted:
            raise _RecordingError(f"recording branch rejected EOS for {self.path}")

    def _wait_for_sink_eos(self, deadline: float) -> None:
        gst = get_gst()
        while True:
            remaining_ns = int(max(0.0, deadline - time.monotonic()) * 1_000_000_000)
            if remaining_ns == 0:
                raise PipelineTimeoutError(f"recording finalization timed out: {self.path}")
            message = self._bus.timed_pop_filtered(  # type: ignore[attr-defined]
                remaining_ns,
                gst.MessageType.ELEMENT | gst.MessageType.ERROR,
            )
            if message is None:
                raise PipelineTimeoutError(f"recording finalization timed out: {self.path}")
            if message.type == gst.MessageType.ERROR:
                if not any(message.src is source for source in self._error_sources):
                    continue
                error, debug = message.parse_error()
                cause = error if isinstance(error, BaseException) else RuntimeError(str(error))
                raise _RecordingError(f"recording branch failed during finalization: {debug}") from cause
            structure = message.get_structure()
            if structure is None or structure.get_name() != "GstBinForwarded":
                continue
            child = structure.get_value("message")
            if child is not None and child.type == gst.MessageType.EOS and child.src is self._sink.impl:
                return

    def _publish(self) -> None:
        try:
            if self._overwrite:
                os.replace(self._temporary_path, self.path)
            else:
                os.link(self._temporary_path, self.path)
                self._temporary_path.unlink()
        except Exception as error:
            raise _RecordingError(f"failed to publish recording file {self.path}") from error


def _prepare_temporary_path(path: Path, *, overwrite: bool) -> Path:
    try:
        validate_recording_path(path, overwrite=overwrite)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    except Exception as error:
        raise _RecordingError(f"failed to open recording segment {path}") from error
    os.close(descriptor)
    return Path(temporary)
