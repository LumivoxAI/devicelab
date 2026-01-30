"""Shared public-volume state for pipeline scenario objects."""

from __future__ import annotations

from typing import Protocol
from threading import RLock
from collections.abc import Callable

from lumivox_devicelab.state import PipelineState
from lumivox_devicelab.errors import PipelineStateError
from lumivox_devicelab._validation import validate_volume


class _VolumeElement(Protocol):
    def set_volume(self, volume: float) -> None: ...


class _VolumeController:
    """Store desired gain across graph replacements and apply it to active graphs."""

    def __init__(self, volume: object) -> None:
        self._volume_lock = RLock()
        self._volume = validate_volume(volume)
        self._volume_element: _VolumeElement | None = None

    @property
    def volume(self) -> float:
        with self._volume_lock:
            return self._volume

    def _set_volume_element(self, element: _VolumeElement) -> None:
        with self._volume_lock:
            element.set_volume(self._volume)
            self._volume_element = element

    def _set_volume(self, volume: object, runtime: _VolumeRuntime) -> None:
        validated = validate_volume(volume)
        state = runtime.state
        if state is PipelineState.CREATED:
            with self._volume_lock:
                self._volume = validated
            return
        if state is not PipelineState.RUNNING:
            raise PipelineStateError(f"cannot set volume in state {state.value}")
        with self._volume_lock:
            self._volume = validated
        runtime.use_graph(lambda pipeline: self._apply_volume())

    def _apply_volume(self) -> None:
        with self._volume_lock:
            if self._volume_element is None:
                raise PipelineStateError("pipeline volume element is unavailable")
            self._volume_element.set_volume(self._volume)


class _VolumeRuntime(Protocol):
    @property
    def state(self) -> PipelineState: ...

    def use_graph(self, operation: Callable[[object], object]) -> object: ...
