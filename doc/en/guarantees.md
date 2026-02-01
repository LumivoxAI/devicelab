# Guarantees And Limitations

[Documentation index](README.md) | [Русская версия](../ru/guarantees.md)

This document distinguishes behavior applications may rely on from behavior
they must handle themselves.

## Audio Data

Devicelab guarantees one public representation:

- NumPy `ndarray` with dtype equal to `numpy.dtype("<i2")`;
- interleaved signed PCM `S16LE`;
- mono shape `(frames,)`;
- multichannel shape `(frames, channels)`;
- at least one frame in every submitted or delivered buffer;
- exact sample rate and channel count from the pipeline's `AudioFormat`.

Complete media graphs normalize through audio conversion, resampling, and exact
caps. Dithering and noise shaping are disabled; the resampler uses Kaiser
quality 4. This is format normalization, not application DSP.

Captured arrays are independent writable copies owned by the receiver. A
frozen `CapturedChunk` prevents field replacement but does not make its array
read-only. Playback callers retain ownership and must not mutate an input array
until `submit()` returns.

## Threading Model

### Capture

- Exactly one delivery worker invokes a handler's `on_start`, `on_chunk`,
  `on_restart`, and `on_stop` callbacks.
- Calls are serial and never overlap for one pipeline.
- Callbacks do not run on a GStreamer streaming thread, the lifecycle caller's
  thread, or an asyncio event loop.
- `on_start()` completes before `start()` succeeds.
- `on_restart()` completes before resumed chunks are delivered.
- After successful `on_start()`, `on_stop()` runs after graph access is closed
  and GStreamer is in `NULL`.
- Handler exceptions fail the pipeline and are retained.

If startup fails or is cancelled before handler activation, or if `on_start()`
raises, no `on_stop()` callback is guaranteed.

Callbacks must return cooperatively. Python cannot terminate arbitrary user
code. Logical stop and GStreamer cleanup have bounded waits, but a callback
thread stuck in application code can outlive pipeline teardown and delay
process exit. The library therefore cannot guarantee bounded process shutdown
when user callbacks are non-cooperative.

A callback may request `stop()` without deadlocking itself. Such a worker-side
call requests shutdown but cannot synchronously join its own thread. An
external caller should perform any required bounded wait.

### Playback

- `submit()` is thread-safe.
- Complete calls are serialized under one operation boundary.
- No fairness or call-arrival ordering is promised among threads waiting for
  the internal lock.
- Graceful stop waits behind an already accepted submission and rejects
  submissions after the state transition.
- Immediate stop does not wait for this boundary and may interrupt a call.
- `state` and `failure` are safe observation properties.

Most other operations should be treated as lifecycle operations rather than a
general concurrently composable API. Coordinate ownership of each single-use
pipeline in application code.

## Buffering And Real-Time Behavior

Live capture and recording queues are bounded. No capture path intentionally
accumulates unlimited Python or GStreamer buffers.

- Live microphone and real-time file capture use `DROP_OLD`: slow handlers lose
  old audio and retain recent audio.
- Fast file capture blocks against bounded queues and delivers every valid
  frame at the handler's sustainable rate.
- Speaker AppSrc targets approximately 250 ms of byte capacity after integer
  rounding and does not intentionally drop accepted audio. At extremely low
  sample rates, that calculation can produce zero, which GStreamer treats as
  unlimited; such formats are outside the bounded-playback guarantee.
- Recording branches block when full and can backpressure their companion live
  branch.

These are buffering guarantees, not hard real-time scheduling guarantees.
Python, GStreamer, the OS scheduler, hardware, and PipeWire may add latency.
Queue durations are capacity targets based on configured PCM; they are not an
end-to-end latency SLA.

Playback submissions are divided into at most
`max(1, floor(sample_rate * 0.02))` frames. This is at most 20 ms only at sample
rates of 50 Hz and above.

## Timestamps And Continuity

Capture timestamps derive from GStreamer segment-converted running time and one
wall/pipeline-clock calibration per generation:

```text
captured_at_ns = wall_time_anchor_ns
               + running_time_ns
               - running_time_anchor_ns
```

The wall-clock anchor is the integer midpoint of wall-time reads bracketing the
pipeline-clock read. Missing timestamps are never fabricated. A malformed or
untimed packet is dropped, and the next delivered chunk is discontinuous.

Use `(generation, running_time_ns)` to order data across microphone recovery.
Do not assume `captured_at_ns` tracks processing time during fast file replay.
Do not assume adjacent callbacks contain continuous audio unless
`discontinuity` is false and generation is unchanged.

## Lifecycle And Failure

- Pipelines are single-use and cannot restart after `stopped`.
- Construction validates configuration but generally defers I/O and graph
  construction to `start()`.
- `stop()` is idempotent; stopping from `created` transitions directly to
  `stopped`.
- `wait()` from `created` is a state error.
- Stop during startup cancels startup; the starting caller receives a
  non-retained state error unless an actual failure also occurred.
- Start and stop timeouts are terminal and trigger forced best-effort teardown.
- A wait timeout ends only that wait and does not stop or fail the pipeline.
- Teardown reaches `stopped` even after failure.
- The first fatal error is authoritative; later errors are secondary.
- Repeated stop after a failure raises the retained failure again.

Bounded teardown prevents workers from accessing a released GStreamer graph.
It cannot guarantee completion of blocked arbitrary application callback code.

## Devices

Discovery guarantees immutable point-in-time snapshots and preserves the
correlation between PCM format, supported sample rates/ranges, and channel
counts/ranges. Unsupported caps entries can be omitted while the device remains
visible.

Device IDs are based on PipeWire `node.name`, not transient `object.serial`.
They are persistent on a best-effort basis, not globally permanent. Device
profiles and local PipeWire rules can change them. Startup always resolves the
configured ID again and never falls back to a default device.

There is no hotplug subscription. Discovery and startup can race with device
changes; applications must refresh and retry with a new pipeline when needed.

## Recovery

Only live microphone capture recovers automatically, and only for the
conditions listed in [Pipelines](pipelines.md). File capture and speaker
playback fail terminally. Microphone recovery has a finite retry budget; it is
not an availability guarantee.

## Recording

- WAV and FLAC are selected by extension.
- Parent directories are not created automatically.
- Output is atomically published after successful encoder finalization.
- Collision protection is rechecked at publication.
- A blocking recording branch may increase live latency.
- Graceful speaker stop records every successfully accepted frame after volume adjustment.
- Immediate stop may discard audio and does not publish an incomplete speaker
  recording.
- Recording is not a transaction across microphone generations: each
  generation is an independently published file.

## Explicit Non-Guarantees

Devicelab does not guarantee:

- hard real-time deadlines or a fixed end-to-end latency;
- lossless live capture when handlers or recording are slow;
- permanent device IDs across PipeWire configuration changes;
- automatic fallback, hotplug monitoring, or migration between devices;
- callback cancellation when user code does not return;
- native async cancellation or event-loop integration;
- synchronization between separate pipeline objects beyond the timestamps and
  backpressure described here;
- support for formats other than public PCM S16LE, or media files other than
  WAV/FLAC input and recording;
- application-level DSP such as VAD, echo cancellation, speech recognition, or
  synthesis.
