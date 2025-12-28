"""Small owner for a single internal GStreamer pipeline graph."""

from __future__ import annotations

from typing import Any, TypeVar
from threading import Lock
from dataclasses import dataclass
from collections.abc import Callable

from lumivox_core.logger import Logger

from .runtime import GStreamerElementError, get_gst
from .elements.base import BaseElement
from .elements.flow import Tee

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class _RequestPad:
    element: Any
    pad: Any


class _PipelineGraph:
    """Own pipeline membership and request pads for one graph generation."""

    def __init__(self, *, logger: Logger, name: str | None = None) -> None:
        self._logger = logger.bind(module="devicelab")
        self._lock = Lock()
        self._cleanup_lock = Lock()
        self._released = False
        self._null_reached = False
        self._elements: list[BaseElement] = []
        self._request_pads: list[_RequestPad] = []

        gst = get_gst()
        self._gst = gst
        self._pipeline: Any | None = gst.Pipeline.new(name)
        if self._pipeline is None:
            raise GStreamerElementError("Failed to create GStreamer pipeline")
        self._logger.debug("gstreamer_graph_created", pipeline_name=name)

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    @property
    def cleanup_complete(self) -> bool:
        with self._lock:
            return self._pipeline is None

    def use(self, operation: Callable[[Any], _T]) -> _T:
        """Start one operation unless graph shutdown has begun."""
        with self._lock:
            self._ensure_open()
            assert self._pipeline is not None
            pipeline = self._pipeline
        return operation(pipeline)

    def add(self, *elements: BaseElement) -> None:
        """Add elements and retain ownership of every successful addition."""
        with self._lock:
            self._ensure_open()
            assert self._pipeline is not None
            for element in elements:
                try:
                    added = self._pipeline.add(element.impl)
                except Exception as error:
                    raise GStreamerElementError(f"Failed to add {element} to GStreamer pipeline") from error
                if not added:
                    raise GStreamerElementError(f"Failed to add {element} to GStreamer pipeline")
                self._elements.append(element)

    def link(self, first: BaseElement, second: BaseElement, *remaining: BaseElement) -> BaseElement:
        """Link a static-pad chain and return its final element."""
        with self._lock:
            self._ensure_open()
            current = first
            for next_element in (second, *remaining):
                self._link_pair(current, next_element)
                current = next_element
            return current

    def branch(self, tee: Tee, first: BaseElement, *remaining: BaseElement) -> BaseElement:
        """Request and own a tee source pad, then link one downstream branch."""
        with self._lock:
            self._ensure_open()
            template = tee.impl.get_pad_template("src_%u")
            if template is None:
                raise GStreamerElementError(f"Failed to get request-pad template from {tee}")
            try:
                source_pad = tee.impl.request_pad(template, None, None)
            except Exception as error:
                raise GStreamerElementError(f"Failed to request source pad from {tee}") from error
            if source_pad is None:
                raise GStreamerElementError(f"Failed to request source pad from {tee}")

            # Record ownership immediately so any later branch-build failure is
            # recoverable through the graph's common partial cleanup path.
            self._request_pads.append(_RequestPad(tee.impl, source_pad))
            sink_pad = first.impl.get_static_pad("sink")
            if sink_pad is None:
                raise GStreamerElementError(f"Failed to get sink pad from {first}")
            try:
                linked = source_pad.link(sink_pad)
            except Exception as error:
                raise GStreamerElementError(f"Failed to link {tee} -> {first}") from error
            if linked != self._gst.PadLinkReturn.OK:
                raise GStreamerElementError(f"Failed to link {tee} -> {first}")

            current = first
            for next_element in remaining:
                self._link_pair(current, next_element)
                current = next_element
            return current

    def close(self) -> tuple[BaseException, ...]:
        """Reject new operations and drive the pipeline to NULL.

        The state call deliberately runs outside the access lock: setting NULL
        must be able to unblock an operation that started before shutdown.
        """
        with self._lock:
            self._released = True
            pipeline = self._pipeline
            null_reached = self._null_reached
        if pipeline is None or null_reached:
            return ()

        errors: list[BaseException] = []
        try:
            result = pipeline.set_state(self._gst.State.NULL)
            if result == self._gst.StateChangeReturn.FAILURE:
                raise GStreamerElementError("Failed to set GStreamer pipeline to NULL")
            with self._lock:
                self._null_reached = True
        except Exception as error:
            self._record_cleanup_error(errors, "set_null", error)
        return tuple(errors)

    def release(self) -> tuple[BaseException, ...]:
        """Close the graph and best-effort release resources after workers stop."""
        with self._cleanup_lock:
            errors = list(self.close())
            with self._lock:
                pipeline = self._pipeline
                null_reached = self._null_reached
                if pipeline is None:
                    return tuple(errors)
                if not null_reached:
                    return tuple(errors)

                remaining_pads: list[_RequestPad] = []
                for requested in reversed(self._request_pads):
                    try:
                        requested.element.release_request_pad(requested.pad)
                    except Exception as error:
                        remaining_pads.append(requested)
                        self._record_cleanup_error(errors, "release_request_pad", error)
                self._request_pads = list(reversed(remaining_pads))

                pad_owners = {id(requested.element) for requested in self._request_pads}
                remaining_elements: list[BaseElement] = []
                for element in reversed(self._elements):
                    if id(element.impl) in pad_owners:
                        remaining_elements.append(element)
                        continue
                    try:
                        if not pipeline.remove(element.impl):
                            raise GStreamerElementError(f"Failed to remove {element} from GStreamer pipeline")
                    except Exception as error:
                        remaining_elements.append(element)
                        self._record_cleanup_error(errors, "remove_element", error)
                self._elements = list(reversed(remaining_elements))

                if not self._request_pads and not self._elements:
                    self._pipeline = None
                    self._logger.debug("gstreamer_graph_released")
                return tuple(errors)

    def _ensure_open(self) -> None:
        if self._released:
            raise GStreamerElementError("GStreamer graph has been released")

    @staticmethod
    def _link_pair(first: BaseElement, second: BaseElement) -> None:
        try:
            linked = first.impl.link(second.impl)
        except Exception as error:
            raise GStreamerElementError(f"Failed to link {first} -> {second}") from error
        if not linked:
            raise GStreamerElementError(f"Failed to link {first} -> {second}")

    def _record_cleanup_error(self, errors: list[BaseException], operation: str, error: BaseException) -> None:
        errors.append(error)
        self._logger.warning("gstreamer_graph_cleanup_failed", operation=operation, error=str(error))
