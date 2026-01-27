# Public API Reference

[Documentation index](README.md) | [Русская версия](../ru/api-reference.md)

All supported names are imported from `lumivox_devicelab`. Constructor arguments
shown after `*` are keyword-only. Public configuration and discovery dataclasses
are frozen and slotted.

## Audio Configuration

### `AudioFormat`

```python
AudioFormat(sample_rate: int, channels: int)
```

Defines the exact normalized sample rate and channel count. Both values must be
positive integers no greater than `2**31 - 1`; `bool` is rejected. Public sample
storage is always PCM S16LE regardless of a device's native format.

### `ChannelSelection`

```python
ChannelSelection(source_channels: int, mapping: tuple[int, ...])
```

Microphone-only source channel selection. `source_channels` is positive.
`mapping` is copied to an immutable tuple, must be nonempty, and contains source
indices in `[0, source_channels)`. Repetition is allowed. A microphone pipeline
requires `len(mapping) == audio_format.channels`.

## Capture Handler And Data

### `CaptureContext`

```python
CaptureContext(
    audio_format: AudioFormat,
    generation: int,
    wall_time_anchor_ns: int,
    running_time_anchor_ns: int,
)
```

Describes one capture generation. Numeric fields are nonnegative integers.
Initial `generation` is zero; successful microphone graph replacement advances
it. The two anchors define conversion from running time to wall-clock time.

### `CapturedChunk`

```python
CapturedChunk(
    samples: numpy.ndarray,
    generation: int,
    captured_at_ns: int,
    running_time_ns: int,
    discontinuity: bool,
)
```

Contains at least one frame of PCM S16LE. Construction makes an independent,
writable copy of `samples`. Time and generation fields are nonnegative.
`discontinuity` is an exact boolean and warns that continuity with the preceding
delivered chunk must not be assumed.

### `CaptureHandler`

```python
class CaptureHandler:
    def on_start(self, context: CaptureContext) -> None: ...
    def on_chunk(self, chunk: CapturedChunk) -> None: ...
    def on_restart(self, context: CaptureContext, cause: PipelineError) -> None: ...
    def on_stop(self, context: CaptureContext, cause: PipelineError | None) -> None: ...
```

`on_chunk()` is abstract; other methods default to no-ops. One delivery worker
calls them serially in lifecycle order. `on_restart()` is microphone-specific.
Exceptions fail the owning pipeline. If `on_start()` does not return
successfully, `on_stop()` is not guaranteed.

## Device Discovery

### `PcmSampleKind`

A `StrEnum` with values `SIGNED_INTEGER = "signed_integer"`,
`UNSIGNED_INTEGER = "unsigned_integer"`, and `FLOAT = "float"`.

### `ByteOrder`

A `StrEnum` with values `LITTLE = "little"`, `BIG = "big"`, and
`NOT_APPLICABLE = "not_applicable"`.

### `PcmFormat`

```python
PcmFormat(
    kind: PcmSampleKind,
    significant_bits: int,
    storage_bits: int,
    byte_order: ByteOrder,
)
```

Describes a native capability format. Enum instances are required. Bit counts
are positive, and `storage_bits >= significant_bits`.

### `IntRange`

```python
IntRange(minimum: int, maximum: int, step: int = 1)
```

An inclusive stepped positive-integer range. `maximum >= minimum`, and the step
must reach `maximum` exactly from `minimum`.

### `AudioCapability`

```python
AudioCapability(
    format: PcmFormat,
    sample_rates: tuple[int | IntRange, ...],
    channel_counts: tuple[int | IntRange, ...],
)
```

Preserves the relationship between one PCM format and its valid rates and
channel counts. Both collections are copied to nonempty tuples.

### `AudioDevice`

```python
AudioDevice(
    id: str,
    name: str,
    is_default: bool,
    capabilities: tuple[AudioCapability, ...],
)
```

`id` and `name` are nonempty. Capabilities are copied to a tuple and may be
empty if no supported caps entry could be represented. `id` is based on
PipeWire `node.name`; `name` is the user-facing description.

### `DeviceSnapshot`

```python
DeviceSnapshot(
    devices: tuple[AudioDevice, ...],
    default: AudioDevice | None,
)
```

An immutable point-in-time collection. Discovery guarantees unique IDs and at
most one default. Empty snapshots and snapshots with no default are valid.

### Discovery clients

```python
MicrophoneDeviceDiscovery(*, logger: Logger)
SpeakerDeviceDiscovery(*, logger: Logger)

snapshot() -> DeviceSnapshot
```

Each `snapshot()` call synchronously starts a GStreamer PipeWire provider,
collects source or sink devices, and stops the provider. It raises `DeviceError`
for provider and inconsistent metadata failures. There is no live subscription.

## Pipeline Lifecycle

### `PipelineState`

A `StrEnum` with `CREATED`, `STARTING`, `RUNNING`, `STOPPING`, and `STOPPED`
whose string values are their lowercase names.

Every public pipeline has:

```python
@property
state -> PipelineState

@property
failure -> PipelineError | None

start(*, timeout: float = 10.0) -> None
stop(*, immediate: bool = False, timeout: float = 10.0) -> None
wait(*, timeout: float | None = None) -> None
```

Timeouts must be finite positive real numbers, excluding `bool`. Only
`wait(timeout=None)` permits an unbounded wait.

## Pipeline Classes

### `MicrophoneCapturePipeline`

```python
MicrophoneCapturePipeline(
    *,
    logger: Logger,
    handler: CaptureHandler,
    audio_format: AudioFormat,
    device_id: str,
    channel_selection: ChannelSelection | None = None,
    record_to: str | os.PathLike[str] | None = None,
    overwrite: bool = False,
)
```

Captures one selected PipeWire microphone. `device_id` must be nonempty.
Optional recording supports WAV or FLAC. `overwrite=True` requires `record_to`.
Device lookup and graph construction are deferred to `start()`.

### `FileReplayMode`

A `StrEnum` with `REALTIME = "realtime"` and
`AS_FAST_AS_POSSIBLE = "as_fast_as_possible"`. Pass the enum member, not its
string value.

### `FileCapturePipeline`

```python
FileCapturePipeline(
    *,
    logger: Logger,
    handler: CaptureHandler,
    audio_format: AudioFormat,
    path: str | os.PathLike[str],
    replay_mode: FileReplayMode,
)
```

Reads WAV or FLAC selected by the path extension. File access is deferred to
`start()`. File capture has no `record_to`, channel selection, or recovery.

### `SpeakerPlaybackPipeline`

```python
SpeakerPlaybackPipeline(
    *,
    logger: Logger,
    audio_format: AudioFormat,
    device_id: str,
    record_to: str | os.PathLike[str] | None = None,
    overwrite: bool = False,
)

submit(data: numpy.ndarray) -> None
```

Plays PCM through one selected PipeWire speaker. `submit()` validates exact
dtype and shape, then blocks as needed against bounded staging. It is legal only
while running. At extremely low sample rates the integer-rounded AppSrc byte
limit can become zero, so bounded staging is not guaranteed for those formats.
Return means AppSrc accepted the data, not that physical playback completed.
`wait()` has no natural speaker completion boundary; call `stop()` to drain.
Optional recording supports WAV or FLAC.

## Error Hierarchy

```text
Exception
└── DevicelabError
    ├── DeviceError
    │   └── DeviceNotFoundError
    └── PipelineError
        ├── PipelineStateError
        ├── PipelineTimeoutError (also TimeoutError)
        └── PlaybackSubmissionError
```

`DevicelabError` is the common base for public device and pipeline errors.
Normal argument validation still raises standard `TypeError` or `ValueError`.

### `PipelineError`

```python
PipelineError(message: str, *, cause: BaseException | None = None)
```

The cause is exposed through standard `__cause__`. `secondary_errors` returns a
tuple snapshot of later failures that did not replace the primary one.

### `PipelineStateError`

Raised for invalid lifecycle operations, such as starting twice or waiting from
`created`.

### `PipelineTimeoutError`

Also inherits `TimeoutError`. Start and stop timeout failures are retained;
wait timeout failures are local to that wait call.

### `PlaybackSubmissionError`

```python
PlaybackSubmissionError(
    message: str,
    *,
    accepted_frames: int,
    cause: BaseException | None = None,
)
```

Reports how many leading frames from a split submission were successfully
accepted before interruption.

### Device errors

`DeviceError` represents discovery or metadata failures.
`DeviceNotFoundError` represents an unresolved device ID. When lookup fails
inside pipeline startup, it is normally available as the cause of the retained
`PipelineError`.

## Integer Validation

Unless a field is explicitly nonnegative, integer fields in the configuration
and discovery value types above require Python `int` values in
`[1, 2**31 - 1]`; `bool` is rejected. Bare sample-rate and channel-count choices
inside `AudioCapability` follow the same rule.
