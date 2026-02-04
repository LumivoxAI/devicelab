# Development Guide

[Documentation index](README.md) | [Русская версия](../ru/development.md)

## Repository Setup

Install the system packages from [Installation](installation.md), plus
[`uv`](https://docs.astral.sh/uv/) and
[`just`](https://github.com/casey/just). Then run from the repository root:

```shell
just postclone
```

This creates `.venv` from `/usr/bin/python3` with `--system-site-packages` so
PyGObject and GStreamer bindings installed by the OS remain visible. It then
synchronizes all dependency groups and extras from `uv.lock`.

Use `just sync` after ordinary dependency changes. Use `just lock` only when
intentionally updating the committed lockfile.

## Source Layout

```text
src/lumivox_devicelab/   public contracts and implementation
  _gstreamer/            internal runtime, graphs, elements, and recovery
examples/                runnable source examples
tests/                   pure, controlled GStreamer, and hardware tests
.agent/                  current project and architecture contracts
```

Read `.agent/project.md` and `.agent/architecture.md` before implementation
changes. They define scope and behavioral invariants. `.agent/tasks/README.md`
contains the active task-document workflow.

## Architecture Boundaries

The public API is scenario-oriented. Do not expose GStreamer or PipeWire
objects, or create a speculative backend plugin system. Current internal layers
cover:

- GStreamer initialization and validated element creation;
- graph assembly and request-pad ownership;
- lifecycle, bus monitoring, cancellation, workers, and errors;
- capture delivery and microphone recovery;
- recording branches and atomic publication;
- PipeWire discovery and source/sink targeting.

Preserve the real-time contract: all live-path queues are bounded and every
overflow policy is explicit. Public audio remains interleaved NumPy PCM S16LE.
Objects that log accept an explicit Lumivox Core `Logger` and bind
`module="devicelab"`; implementation code must not configure logging.

Update `.agent/project.md` or `.agent/architecture.md` in the same change when
public API, threading, timestamps, buffering, error handling, backend
boundaries, or lifecycle behavior changes. Keep `doc/en` and `doc/ru`
consistent when user-visible behavior changes.

## Common Commands

List all recipes with `just`. The main workflows are:

| Command | Purpose |
| --- | --- |
| `just postclone` | Create and fully synchronize the development environment |
| `just sync` | Synchronize project and development dependencies |
| `just lock` | Intentionally update `uv.lock` |
| `just lock_check` | Check lockfile consistency without changing it |
| `just fmt` | Format the repository with Ruff |
| `just fmt_check` | Check formatting without edits |
| `just lint` | Run Ruff lint checks |
| `just lint_fix` | Apply safe Ruff lint fixes |
| `just typecheck` | Run strict mypy checks |
| `just test` | Run tests; hardware tests remain opt-in |
| `just test_pure` | Run tests requiring neither GStreamer nor hardware |
| `just test_gstreamer` | Run controlled non-hardware GStreamer tests |
| `just test_hardware` | Run opt-in physical PipeWire tests |
| `just test_all` | Test with all dependency groups and extras, not hardware opt-in |
| `just build` | Rebuild wheel and source distribution without local uv sources |
| `just precommit` | Run formatting, lint, types, tests, and lockfile checks |

`just precommit` is the complete required local check. Public export or package
layout changes also require `just build` and inspection of both artifacts for
caches, prototypes, and unsupported files.

## Test Layers

### Pure Tests

```shell
just test_pure
```

Use deterministic fakes for lifecycle races and inject monotonic time for
recovery-window behavior. Avoid wall-time performance assertions.

### Controlled GStreamer Tests

```shell
just test_gstreamer
```

These use controlled, non-PipeWire graphs where possible. They skip with a
specific reason when PyGObject, GStreamer 1.24+, or required factories are
unavailable.

### Physical PipeWire Tests

Hardware access is explicit and requires stable PipeWire `node.name` values:

```shell
export LUMIVOX_DEVICELAB_MICROPHONE_ID='alsa_input.example'
export LUMIVOX_DEVICELAB_SPEAKER_ID='alsa_output.example'
just test_hardware
```

Without the `--run-pipewire-hardware` option embedded in `just test_hardware`,
hardware-marked tests are skipped. Each device category also skips independently
if its environment variable is absent. Speaker tests produce audible output and
require an active user PipeWire/WirePlumber session.

Pytest markers are:

- `gstreamer`: controlled test requiring bindings and plugins;
- `pipewire_hardware(device)`: opt-in test for `microphone` or `speaker`.

## Adding Or Changing A Pipeline

1. Define configuration and immutable public data without backend objects.
2. Validate deterministic arguments in the constructor; defer device, file,
   and GStreamer runtime work to `start()`.
3. Specify queue capacity and whether overflow blocks or drops old data.
4. Specify graceful and immediate shutdown boundaries.
5. Ensure asynchronous errors unblock public operations and retain the first
   fatal cause.
6. Ensure cancellation closes graph access before graph release and prevents
   late worker calls into GStreamer.
7. Test success, invalid states, startup cancellation, timeout, worker failure,
   bus error, EOS, teardown races, and repeated stop.
8. Update architecture and both documentation languages.
9. Run `just precommit`; run `just build` when package layout or exports change.

## Style And Tooling

- Supported syntax baseline: Python 3.13.
- Ruff line length: 120, double quotes, lint families `E`, `F`, and `I`.
- Mypy runs in strict mode over `src/lumivox_devicelab`, `tests`, and
  `examples`.
- The package is typed and includes `py.typed`.
- Public exports are defined explicitly in `lumivox_devicelab.__all__` and
  covered by tests.

Examples are not installed as entry points. Keep them directly runnable from a
checkout and ensure their `--help` path does not require working audio hardware.
