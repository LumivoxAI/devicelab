# Lumivox Devicelab: LLM Reference

Use this contract to write application code with `lumivox_devicelab`. Import only
from that package; GStreamer and PipeWire are private implementation details.

## Core Rules

- Linux, PipeWire, and WirePlumber only. The API is synchronous; no asyncio API.
- Public audio is an interleaved PCM `numpy.ndarray`, exactly `dtype=numpy.dtype("<i2")`.
  Mono: `(frames,)`; multi-channel: `(frames, channels)`. Arrays must be nonempty
  and match the configured `AudioFormat` rate and channel count.
- Pipelines are single-use: `CREATED -> STARTING -> RUNNING -> STOPPING -> STOPPED`.
  Create a new instance after stopping.
- Discovery and pipelines require keyword-only `logger: lumivox_core.logger.Logger`.
  Constructors validate configuration; device/file access normally begins in `start()`.
- Use `AudioDevice.id`, never `name`. There is no automatic default-device fallback
  or hotplug subscription.

```python
from lumivox_core.logger import LoggingConfig, configure_logging, get_logger

configure_logging(LoggingConfig(application="my-application"))
logger = get_logger(component="audio")
```

## Public API

Import these directly from `lumivox_devicelab`.

```python
AudioFormat(sample_rate: int, channels: int)
ChannelSelection(source_channels: int, mapping: tuple[int, ...])

MicrophoneDeviceDiscovery(*, logger: Logger)
SpeakerDeviceDiscovery(*, logger: Logger)
# Both: snapshot() -> DeviceSnapshot

MicrophoneCapturePipeline(
    *, logger: Logger, handler: CaptureHandler, audio_format: AudioFormat,
    device_id: str, channel_selection: ChannelSelection | None = None,
    record_to: str | os.PathLike[str] | None = None, overwrite: bool = False,
    volume: float = 1.0,
)
SpeakerPlaybackPipeline(
    *, logger: Logger, audio_format: AudioFormat, device_id: str,
    record_to: str | os.PathLike[str] | None = None, overwrite: bool = False,
    volume: float = 1.0,
)
# Also: submit(data: numpy.ndarray) -> None
FileCapturePipeline(
    *, logger: Logger, handler: CaptureHandler, audio_format: AudioFormat,
    path: str | os.PathLike[str], replay_mode: FileReplayMode, volume: float = 1.0,
)
```

Every pipeline provides:

```python
pipeline.state -> PipelineState
pipeline.failure -> PipelineError | None
pipeline.start(*, timeout: float = 10.0) -> None
pipeline.stop(*, immediate: bool = False, timeout: float = 10.0) -> None
pipeline.wait(*, timeout: float | None = None) -> None
pipeline.volume -> float
pipeline.set_volume(volume: float) -> None
```

Timeouts must be positive finite values; only `wait(timeout=None)` is unbounded.
`PipelineState`: `CREATED`, `STARTING`, `RUNNING`, `STOPPING`, `STOPPED`.
`FileReplayMode`: `REALTIME`, `AS_FAST_AS_POSSIBLE`; pass the enum, not a string.

`volume` is linear gain in `[0.0, 10.0]`: `0.0` is silence, `1.0` preserves
normalized level. Set it in the constructor, before `start()`, or while `RUNNING`.
`set_volume()` in `STARTING`, `STOPPING`, or `STOPPED` raises `PipelineStateError`.
Gain is applied after normalization and before capture delivery, speaker output, and
recording; recordings contain adjusted PCM. Values above `1.0` can clip. Microphone
recovery keeps the current volume.

## Discovery

```python
microphones = MicrophoneDeviceDiscovery(logger=logger).snapshot()
speakers = SpeakerDeviceDiscovery(logger=logger).snapshot()

if not microphones.default and not microphones.devices:
    raise RuntimeError("no microphone")
microphone_id = (microphones.default or microphones.devices[0]).id
```

`DeviceSnapshot.devices` is an immutable, possibly empty tuple; `default` may be
`None`. Snapshots do not refresh. Re-snapshot after device changes, then create a
new pipeline.

`AudioDevice`: `id`, `name`, `is_default`, `capabilities`. Each `AudioCapability`
correlates one `PcmFormat` with `sample_rates` and `channel_counts`; each is an `int`
or inclusive `IntRange(minimum, maximum, step)`. Capabilities are native formats;
pipelines still normalize to the requested `AudioFormat` and S16LE.

`PcmFormat`: `kind`, `significant_bits`, `storage_bits`, `byte_order`.
`PcmSampleKind`: `SIGNED_INTEGER`, `UNSIGNED_INTEGER`, `FLOAT`.
`ByteOrder`: `LITTLE`, `BIG`, `NOT_APPLICABLE`.

## Capture

Subclass `CaptureHandler`; only `on_chunk` is abstract.

```python
class Handler(CaptureHandler):
    def on_start(self, context: CaptureContext) -> None: ...
    def on_chunk(self, chunk: CapturedChunk) -> None: ...
    def on_restart(self, context: CaptureContext, cause: PipelineError) -> None: ...
    def on_stop(self, context: CaptureContext, cause: PipelineError | None) -> None: ...
```

One internal delivery thread invokes callbacks serially. It is neither a GStreamer
thread, the `start()` caller, nor an event loop. `on_start` completes before
`start()` returns; `on_restart` completes before resumed chunks. Any callback
exception fails the pipeline.

Callbacks must return quickly and never perform unbounded/blocking work. Send costly
work to an application-owned bounded queue. `chunk.samples` is already an independent
writable copy and needs no extra copy to cross threads. Lock handler state shared with
other application threads; dispatch UI work through the UI framework's thread-safe API.

`CapturedChunk` fields: `samples`, `generation` (initially `0`), `captured_at_ns`
(first-frame wall-clock time), `running_time_ns` (first-frame pipeline running time),
and `discontinuity`. `CaptureContext` fields: `audio_format`, `generation`,
`wall_time_anchor_ns`, `running_time_anchor_ns`. Per generation:

```text
captured_at_ns = wall_time_anchor_ns + running_time_ns - running_time_anchor_ns
```

Never concatenate chunks across `discontinuity=True` or a generation change. Order
recovered capture by `(generation, running_time_ns)`.

```python
pipeline = MicrophoneCapturePipeline(
    logger=logger, handler=Handler(),
    audio_format=AudioFormat(sample_rate=16_000, channels=1),
    device_id=microphone_id, volume=0.8,
)
try:
    pipeline.start()
    pipeline.wait()
except KeyboardInterrupt:
    pipeline.stop()
```

Live capture uses a bounded `DROP_OLD` queue: a slow handler loses old audio and the
next chunk is discontinuous. Microphone capture can recover automatically: it remains
`RUNNING`, calls `on_restart`, increments `generation`, and marks its first new chunk
discontinuous.

`ChannelSelection` is microphone-only. `mapping[i]` selects the source channel for
output channel `i`; its length must equal `AudioFormat.channels`, and indices may
repeat. Without it, GStreamer performs standard channel conversion.

```python
channel_selection = ChannelSelection(source_channels=4, mapping=(0, 2))
```

## Playback

```python
audio_format = AudioFormat(sample_rate=16_000, channels=1)
speaker = SpeakerPlaybackPipeline(
    logger=logger, audio_format=audio_format, device_id=speaker_id, volume=0.8,
)
speaker.start()
try:
    speaker.submit(np.zeros(16_000, dtype="<i2"))
finally:
    speaker.stop()  # graceful: drain accepted samples
```

For stereo, use `(frames, 2)`, not a flat interleaved array. `submit()` is
thread-safe, allowed only in `RUNNING`, and may block on bounded backpressure. Do not
modify the supplied array until it returns. Return means accepted, not physically
played. Calls are serialized, but concurrent-caller ordering and fairness are not
guaranteed; use one application playback worker when order matters.

`wait()` does not finish naturally for a speaker; another thread must call `stop()`
or a failure must occur. Normal completion: stop producers, wait for their final
`submit()`, then call graceful `stop()`.

`stop(immediate=True)` may discard queued audio or interrupt `submit()`. In that case,
`PlaybackSubmissionError.accepted_frames` is the accepted prefix; do not resend it.

```python
try:
    speaker.submit(samples)
except PlaybackSubmissionError as error:
    remaining = samples[error.accepted_frames:]
    cause = error.__cause__
```

### Microphone To Speaker

Never call potentially blocking `speaker.submit()` from a live-capture callback.
Use one playback worker and an application-owned bounded queue with an explicit
overflow policy. For fresh-audio voice agents, use `DROP_OLD`: on `Full`, remove one
item with `get_nowait()` and retry `put_nowait()`. Start the speaker before the worker
and microphone. On shutdown: stop the microphone, signal and join the worker, then
gracefully stop the speaker; use immediate stop after any failure or stuck worker.

If loss is unacceptable, a bounded blocking queue propagates backpressure but live
capture can still drop audio. Use `FileReplayMode.AS_FAST_AS_POSSIBLE` for a lossless
offline path.

## File Capture

```python
pipeline = FileCapturePipeline(
    logger=logger, handler=Handler(),
    audio_format=AudioFormat(sample_rate=16_000, channels=1),
    path="input.flac", replay_mode=FileReplayMode.AS_FAST_AS_POSSIBLE,
)
pipeline.start()
pipeline.wait()  # normal EOF stops the pipeline
```

Only case-insensitive `.wav` and `.flac` are supported. `REALTIME` follows the media
clock and uses `DROP_OLD` for slow handlers. `AS_FAST_AS_POSSIBLE` skips wall-clock
pacing, blocks on its bounded queue, and delivers every valid frame. File capture has
no recovery, `ChannelSelection`, or `record_to`.

For file-to-speaker with `AS_FAST_AS_POSSIBLE`, `speaker.submit(chunk.samples)` is
safe in the handler because both sides use bounded blocking backpressure. Start the
speaker first; gracefully stop it after EOF.

## Recording

`MicrophoneCapturePipeline` and `SpeakerPlaybackPipeline` accept:

```python
record_to="recordings/audio.wav"  # or .flac
overwrite=False
```

The parent directory must exist. The output is atomically published only after encoder
finalization. An existing target fails when `overwrite=False`; `overwrite=True`
requires `record_to`. Microphone recovery creates `audio.1.wav`, `audio.2.wav`, etc.
Speaker recordings contain adjusted submitted PCM only, not silence for idle periods.
The bounded recording branch blocks for completeness and can add latency. Graceful
speaker stop records every accepted frame; immediate stop can remove an incomplete
temporary file.

## Lifecycle And Errors

- Coordinate `start()` and `stop()` through one application controller.
- `stop()` is graceful and idempotent. Use `immediate=True` for cancellation or when
  draining is unnecessary.
- `wait(timeout=...)` ends only that wait. A `start()` or `stop()` timeout is terminal.
- A terminal failure still ends in `STOPPED`; inspect `pipeline.failure`. Lifecycle
  methods also re-raise the retained failure.
- A callback may request `stop()` but cannot join its own delivery thread; an external
  controller must perform the final bounded wait.
- Python cannot kill a stuck callback. Every callback must finish in bounded time.
- In asyncio, run `snapshot`, `start`, `submit`, `wait`, and `stop` via
  `asyncio.to_thread()` or an executor. Cancelling that task does not stop its active
  synchronous call: another worker must call `stop(immediate=True)` and wait.

```text
DevicelabError
├── DeviceError
│   └── DeviceNotFoundError
└── PipelineError
    ├── PipelineStateError
    ├── PipelineTimeoutError  # also TimeoutError
    └── PlaybackSubmissionError
```

Bad arguments raise `TypeError` or `ValueError`. A `PipelineError` keeps its primary
cause in `__cause__` and later teardown failures in `secondary_errors`. Preserve the
first pipeline failure; best-effort stop related pipelines without replacing it with a
cleanup failure.
