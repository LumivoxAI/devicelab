"""Public Devicelab domain errors."""

from __future__ import annotations

from lumivox_devicelab._validation import validate_non_negative_int


class DevicelabError(Exception):
    """Base class for runtime failures reported by Devicelab."""


class DeviceError(DevicelabError):
    """A device discovery or selection failure."""


class DeviceNotFoundError(DeviceError):
    """The requested stable device identifier could not be resolved."""


class PipelineError(DevicelabError):
    """A pipeline failure that retains its original and secondary causes."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.__cause__ = cause
        self._secondary_errors: list[BaseException] = []

    @property
    def secondary_errors(self) -> tuple[BaseException, ...]:
        """Return failures observed after the authoritative pipeline failure."""
        return tuple(self._secondary_errors)

    def _add_secondary_error(self, error: BaseException) -> None:
        self._secondary_errors.append(error)


class PipelineStateError(PipelineError):
    """An operation is invalid for the pipeline's current lifecycle state."""


class PipelineTimeoutError(PipelineError, TimeoutError):
    """A terminal pipeline operation exceeded its timeout."""


class PlaybackSubmissionError(PipelineError):
    """Playback stopped after accepting only a prefix of a submission."""

    def __init__(
        self,
        message: str,
        *,
        accepted_frames: int,
        cause: BaseException | None = None,
    ) -> None:
        self.accepted_frames = validate_non_negative_int(accepted_frames, name="accepted_frames")
        super().__init__(message, cause=cause)
