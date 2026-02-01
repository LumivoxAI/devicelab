# Pipelines

[Documentation index](README.md) | [Русская версия](../ru/pipelines.md)

Devicelab exposes scenario objects rather than GStreamer elements. It owns each
media graph, its workers, bounded queues, timestamps, device resolution, and
teardown. GStreamer and PipeWire objects remain implementation details.

## Volume

Every pipeline accepts `volume: float = 1.0`, exposes `volume`, and provides
`set_volume(value)`. It uses linear gain: `0.0` is silence, `1.0` is unchanged,
and `10.0` is the maximum supported gain. Set it before `start()` or while the
pipeline is running; setting it during startup, stopping, or after stop raises
`PipelineStateError`. The gain element is after PCM normalization and before
all downstream branches, so handlers, speakers, and WAV/FLAC recordings receive
the adjusted signal. Gains above `1.0` can clip S16LE samples. Microphone
recovery keeps and reapplies the latest value.

## Shared Lifecycle

Every pipeline has a single-use lifecycle:

```text
created -> starting -> running -> stopping -> stopped
```

`start()` builds the graph, moves it to `PLAYING`, starts monitoring and
delivery workers, and waits for scenario readiness. Microphone and file capture
also wait for negotiated output caps and handler startup. Speaker startup does
not require a priming PCM submission; actual downstream negotiation may occur
on the first `submit()`.

A bus watcher observes warnings, errors, state changes, and EOS. Warnings are
logged but are not fatal. Worker exceptions and bus errors become observable
pipeline failures and unblock waiting operations.

Graceful stop establishes an input boundary, propagates EOS, and drains accepted
work. Immediate stop signals cancellation directly. Both paths close graph
access, move GStreamer to `NULL`, and join library workers with bounded waits.

There is no separate failed state. Teardown always ends in `stopped`, while
`failure` records an unsuccessful outcome. The first fatal error remains the
primary error; later cleanup or callback errors appear in `secondary_errors`.

## Microphone Capture

Without explicit channel selection or recording, the graph is:

```text
PipeWire source
  -> audio conversion
  -> resampling
  -> exact S16LE/rate/channel caps
  -> linear volume
  -> bounded 200 ms DROP_OLD queue
  -> one-buffer dropping app sink
  -> Python delivery worker
```

With `ChannelSelection`, an exact source-channel caps filter and channel mapping
are inserted before normalization. With recording, normalized audio passes
through a tee:

```text
normalized audio -> tee
  -> bounded DROP_OLD live branch -> app sink -> handler
  -> bounded 1000 ms BLOCK branch -> WAV/FLAC encoder -> temporary file
```

The live branch favors recent audio if the handler falls behind. The recording
branch favors completeness and may backpressure the whole graph.

### Delivery

The app sink maps each valid sample and copies it into little-endian `int16`
storage. The delivery worker obtains a bounded batch of currently available
packets and combines only contiguous packets. It does not perform unbounded
Python-side prefetch.

A new public chunk begins when the graph reports a discontinuity, a forced
boundary is present, or timestamps differ by more than one output-frame period.
One-frame timestamp rounding is tolerated. Invalid packets are dropped and
force the next chunk to have `discontinuity=True`.

### Automatic Recovery

Only microphone capture automatically restarts. Recoverable conditions while
running are:

- an error emitted by the current `pipewiresrc` element;
- unexpected EOS;
- incompatible capture caps;
- the third buffer-mapping failure in a rolling ten-second window;
- the second malformed-frame or missing-time failure in their shared rolling
  ten-second window.

Events exactly ten seconds old have expired. Recovery allows three replacement
attempts after delays of 0.25, 1, and 4 seconds. A successful replacement does
not immediately restore that budget; the budget resets when another recovery
starts after at least 60 seconds of stable operation.

During recovery, public state remains `running`. Stop cancels the backoff. On a
successful replacement the generation increments, time is recalibrated,
`on_restart()` runs before resumed data, and the first resumed chunk is marked
discontinuous. Handler, recording, graph-invariant, deterministic
configuration, non-source bus, and exhausted-retry failures are terminal.

## File Capture

WAV input uses `wavparse`; FLAC uses `flacparse` and `flacdec`. Both then pass
through the same normalization used by live capture:

```text
file source -> parser/decoder -> conversion -> resampling -> exact S16LE caps -> linear volume
```

`FileReplayMode.REALTIME` adds clock synchronization and bounded `DROP_OLD`
delivery. It behaves like a real-time source and can discard old audio when the
handler is slow.

`FileReplayMode.AS_FAST_AS_POSSIBLE` omits clock pacing and uses bounded blocking
delivery. It delivers every valid frame at the handler's sustainable rate.
Relative media timestamps are equivalent between modes, but wall-clock anchors
naturally differ. In fast mode, `captured_at_ns` is a logical media timeline and
may advance ahead of actual processing wall time.

Natural EOS drains accepted packets and completes successfully. File parsing,
caps, mapping, malformed-frame, and timestamp faults are terminal. File capture
does not perform recovery and cannot add a recording branch.

## Speaker Playback

Without recording, the graph is:

```text
Python S16LE array
  -> bounded app source
  -> audio conversion
  -> resampling
  -> exact S16LE/rate/channel caps
  -> linear volume
  -> PipeWire sink
```

`submit()` splits input into GStreamer buffers no longer than 20 ms. AppSrc
uses at least one frame per buffer, so the 20 ms bound applies at sample rates
of 50 Hz and above. AppSrc staging targets 250 ms of configured PCM after
integer byte rounding and blocks the submitting caller when full. At extremely
low rates the computed byte capacity can be zero, which GStreamer treats as
unlimited; this configuration is outside the bounded-playback guarantee. Audio
accepted by normal operation is not intentionally dropped to reduce latency.

PTS is the later of the preceding successfully pushed buffer's end and the
current pipeline running time. Queued audio therefore stays contiguous. A real
period with no submitted data creates a timestamp gap; Devicelab does not insert
synthetic silence. Timestamp state advances only after a successful push.

With recording, normalized audio passes through a tee:

```text
normalized audio -> tee
  -> bounded 250 ms BLOCK branch -> PipeWire sink
  -> bounded 1000 ms BLOCK branch -> WAV/FLAC encoder -> temporary file
```

Graceful stop shares an operation boundary with `submit()`: it waits for an
already accepted complete call, rejects later calls, sends EOS, drains playback,
and publishes the recording. Immediate stop does not wait for that boundary and
may interrupt a submission or discard staged audio.

`submit()` returns after AppSrc accepts the array, before physical playback is
necessarily complete. Speaker playback has no natural completion boundary;
`wait()` needs another thread to stop the pipeline or a failure to terminate it.

## Recording Publication

Recording output is first written to a hidden temporary file owned by the
pipeline. The target is atomically published only after encoder EOS. Without
overwrite, publication cannot replace a target that appeared concurrently;
with overwrite, publication atomically replaces the target.

Microphone recovery finalizes the current generation and opens
`<stem>.<generation><suffix>`. Collision checks apply independently to every
segment. Graceful speaker completion guarantees that all successfully accepted
PCM appears in its recording. Immediate speaker stop or asynchronous speaker
failure removes the unpublished temporary file.

Speaker recordings contain submitted PCM after volume adjustment only. Idle
timestamp gaps do not add silence.

## Composing File Capture With Playback

A file can be decoded and normalized through a handler that submits each chunk
to a speaker:

```python
from lumivox_devicelab import CaptureHandler

class PlaybackHandler(CaptureHandler):
    def __init__(self, speaker):
        self._speaker = speaker

    def on_chunk(self, chunk):
        self._speaker.submit(chunk.samples)
```

Start the speaker first, then start and wait for file capture. Stop the speaker
gracefully after file EOS. `submit()` backpressure naturally limits fast file
capture because both sides use bounded blocking paths. On cancellation or
failure, immediately stop both pipelines and preserve the first application
error. See `examples/record_playback_gui.py` for a complete orchestration
example.
