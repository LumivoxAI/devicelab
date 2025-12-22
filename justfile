# Repository development workflows.

default:
    @just --list

# Create or synchronize the complete local development environment.
postclone:
    uv sync --all-groups --all-extras

# Synchronize the default project and development dependencies.
sync:
    uv sync

# Resolve and update the committed dependency lockfile.
lock:
    uv lock

# Verify that the lockfile matches pyproject.toml.
lock_check:
    uv lock --check

fmt:
    uv run ruff format .

fmt_check:
    uv run ruff format --check .

lint:
    uv run ruff check .

lint_fix:
    uv run ruff check --fix .

typecheck:
    uv run mypy

test:
    uv run pytest

# Run pure tests that do not require GStreamer or physical audio devices.
test_pure:
    uv run pytest -m "not gstreamer and not pipewire_hardware"

# Run controlled GStreamer tests; unavailable system dependencies are reported as skips.
test_gstreamer:
    uv run pytest -m gstreamer

# Run opt-in tests against stable PipeWire device IDs from the environment.
test_hardware:
    uv run pytest -m pipewire_hardware --run-pipewire-hardware

# Test with every dependency group and optional feature enabled.
test_all:
    uv run --all-groups --all-extras pytest

# Build artifacts without local uv source overrides.
build:
    uv build --no-sources

# Run the fast, required checks before creating a commit.
precommit: fmt_check lint typecheck test lock_check
