"""Public pipeline lifecycle state."""

from enum import StrEnum


class PipelineState(StrEnum):
    """The single-use lifecycle states shared by Devicelab pipelines."""

    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
