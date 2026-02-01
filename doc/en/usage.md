# Usage Guide

[Documentation index](README.md) | [Русская версия](../ru/usage.md)

All examples assume that `logger` has been created as shown in the
[installation guide](installation.md).

## Discover Devices

Discovery is synchronous and returns an immutable point-in-time snapshot:

```python
from lumivox_devicelab import MicrophoneDeviceDiscovery, SpeakerDeviceDiscovery

microphones = MicrophoneDeviceDiscovery(logger=logger).snapshot()
speakers = SpeakerDeviceDiscovery(logger=logger).snapshot()

for device in microphones.devices:
    marker = " (default)" if device.is_default else ""
    print(f"{device.id}: {device.name}{marker}")
```

Use `device.id`, not the display name, when creating a pipeline. The ID is based
on PipeWire `node.name`; it is normally stable but can change after profile or
local-rule changes. Each pipeline resolves it through a fresh snapshot during
`start()`. A missing ID is an explicit failure and never selects the default
implicitly.

Snapshots do not update after hotplug. Call `snapshot()` again to refresh.

## Handle Captured Audio

Subclass `CaptureHandler`; only `on_chunk()` is required:

```python
from lumivox_devicelab import CapturedChunk, CaptureContext, CaptureHandler, PipelineError

class AudioHandler(CaptureHandler):
    def on_start(self, context: CaptureContext) -> None:
        print("capture started", context.audio_format)

    def on_chunk(self, chunk: CapturedChunk) -> None:
        process_audio(chunk.samples)

    def on_restart(self, context: CaptureContext, cause: PipelineError) -> None:
        print("microphone restarted", context.generation, cause)

    def on_stop(self, context: CaptureContext, cause: PipelineError | None) -> None:
        print("capture stopped", cause)
```

One dedicated delivery worker invokes all callbacks serially. Do not block it
for unbounded work. Copying `chunk.samples` is unnecessary for ownership: every
chunk already owns a writable NumPy allocation. Queue the chunk to your own
bounded worker pool if processing is expensive.

Inspect `chunk.discontinuity` before assuming adjacency with the previous
chunk. It becomes true after dropped or malformed input, timestamp gaps, and
microphone recovery.

## Capture A Microphone

```python
from lumivox_devicelab import AudioFormat, MicrophoneCapturePipeline

pipeline = MicrophoneCapturePipeline(
    logger=logger,
    handler=AudioHandler(),
    audio_format=AudioFormat(sample_rate=16_000, channels=1),
    device_id="alsa_input.example",
)

try:
    pipeline.start()
    pipeline.wait()
except KeyboardInterrupt:
    pipeline.stop()
```

Construction validates arguments but does not access the device. Runtime setup
begins in `start()`. Microphone capture retries a documented subset of source
failures while keeping the public state `running`; see [Pipelines](pipelines.md).

## Adjust Volume

Every capture and playback pipeline accepts a linear `volume` from `0.0` to
`10.0`. `0.0` is silence and `1.0` preserves the source level. It can be set at
construction or changed while running:

```python
pipeline.set_volume(0.6)
current_volume = pipeline.volume
```

The adjusted PCM is delivered to capture handlers, played by speakers, and
written to optional recordings. Gain above `1.0` may clip S16LE samples.

## Select Input Channels

`ChannelSelection` is available only for microphone capture. The mapping index
identifies an output channel and its value identifies the source channel:

```python
from lumivox_devicelab import AudioFormat, ChannelSelection, MicrophoneCapturePipeline

pipeline = MicrophoneCapturePipeline(
    logger=logger,
    handler=AudioHandler(),
    audio_format=AudioFormat(sample_rate=48_000, channels=2),
    device_id="alsa_input.example",
    channel_selection=ChannelSelection(
        source_channels=4,
        mapping=(0, 2),
    ),
)
```

The mapping length must equal the output channel count. Source indices may
repeat, for example `(0, 0)` duplicates source channel zero into stereo.
Negotiation fails if the source cannot provide exactly `source_channels`.
Without a selection, GStreamer performs its standard channel conversion.

## Capture A WAV Or FLAC File

```python
from lumivox_devicelab import AudioFormat, FileCapturePipeline, FileReplayMode

pipeline = FileCapturePipeline(
    logger=logger,
    handler=AudioHandler(),
    audio_format=AudioFormat(sample_rate=16_000, channels=1),
    path="input.flac",
    replay_mode=FileReplayMode.AS_FAST_AS_POSSIBLE,
    volume=0.8,
)
pipeline.start()
pipeline.wait()
```

Choose `REALTIME` to pace data by the media clock. Choose
`AS_FAST_AS_POSSIBLE` for bounded blocking delivery without intentional frame
loss. Natural end-of-file, including a valid empty file, completes normally.
Only WAV and FLAC are supported, selected case-insensitively by extension.

## Play Audio

```python
import numpy as np

from lumivox_devicelab import AudioFormat, SpeakerPlaybackPipeline

audio_format = AudioFormat(sample_rate=16_000, channels=1)
pipeline = SpeakerPlaybackPipeline(
    logger=logger,
    audio_format=audio_format,
    device_id="alsa_output.example",
    volume=0.8,
)

pipeline.start()
pipeline.submit(np.zeros(16_000, dtype="<i2"))
pipeline.stop()
```

`submit()` requires an exact S16LE NumPy array matching `AudioFormat`: mono is
`(frames,)`, multichannel is `(frames, channels)`. Empty arrays are rejected.
Do not mutate the input until `submit()` returns.

Multiple application threads may call `submit()`. Complete calls are
serialized, but ordering among threads waiting for the lock is not guaranteed
to match call-arrival order. The caller blocks when the bounded staging queue is
full; accepted audio is not intentionally dropped.

Return from `submit()` means that AppSrc accepted the complete array, not that
the hardware finished playing it. A speaker has no natural completion event:
`wait()` returns only after another thread calls `stop()` or after a failure.
Call graceful `stop()` to send EOS and drain accepted playback.

If immediate stop or a media failure interrupts a large submission, inspect the
accepted prefix:

```python
from lumivox_devicelab import PlaybackSubmissionError

try:
    pipeline.submit(samples)
except PlaybackSubmissionError as error:
    print("accepted frames:", error.accepted_frames)
    print("cause:", error.__cause__)
```

## Record Capture Or Playback

Microphone and speaker pipelines accept `.wav` or `.flac` targets:

```python
pipeline = MicrophoneCapturePipeline(
    logger=logger,
    handler=AudioHandler(),
    audio_format=AudioFormat(sample_rate=16_000, channels=1),
    device_id="alsa_input.example",
    record_to="recordings/capture.wav",
    overwrite=False,
)
```

The parent directory must already exist. The target is atomically published
only after encoder finalization. Existing targets are rejected unless
`overwrite=True`. Microphone recovery creates `capture.1.wav`,
`capture.2.wav`, and so on. `FileCapturePipeline` cannot record.

Recording uses a bounded blocking branch. This protects file completeness but
can backpressure capture or playback and increase latency.

## Stop And Wait Correctly

All pipelines are single-use:

```text
created -> starting -> running -> stopping -> stopped
```

- `stop()` is graceful and drains accepted work within its timeout.
- `stop(immediate=True)` cancels directly and may discard queued work.
- `wait()` waits for completion; `wait(timeout=...)` does not stop the pipeline
  when it times out.
- `start()` and `stop()` default to a 10-second timeout.
- A start or stop timeout is terminal; a wait timeout affects only that call.
- A stopped pipeline cannot be started again. Create a new object.

If capture startup fails before `on_start()` returns successfully, including an
exception from `on_start()` itself, `on_stop()` is not guaranteed. Clean up
resources allocated before successful startup in the caller's `start()` error
path.

Use `pipeline.state` and `pipeline.failure` for observation. Lifecycle methods
raise retained pipeline failures, so do not rely only on polling `state`.

## Run Repository Examples

Examples are source files and are not installed as console commands:

```shell
uv run python examples/microphone_capture.py
uv run python examples/microphone_capture.py 'alsa_input.example'
uv run python examples/file_capture.py input.wav
uv run python examples/file_capture.py --realtime input.flac
uv run python examples/speaker_playback.py 'alsa_output.example'
uv run python examples/recording.py 'alsa_input.example' output.wav
uv run python examples/record_playback_gui.py
```

Run any script with `--help`. The local NiceGUI demo listens only on
`127.0.0.1:8080` by default and requires development dependencies.
