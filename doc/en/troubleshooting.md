# Troubleshooting

[Documentation index](README.md) | [Русская версия](../ru/troubleshooting.md)

## `No module named gi`

Install PyGObject, GStreamer introspection, and gst-python from the operating
system package manager. A fully isolated virtual environment often cannot see
these system bindings. In a repository checkout, recreate the environment with:

```shell
just postclone
```

This uses `/usr/bin/python3 --system-site-packages`.

## A GStreamer Factory Is Missing

Install the base, good, and PipeWire plugin packages listed in
[Installation](installation.md). Diagnose the environment with:

```shell
just test_gstreamer
gst-inspect-1.0 pipewiresrc
gst-inspect-1.0 pipewiresink
```

WAV/FLAC capture and recording additionally require their parser, decoder, and
encoder factories.

## No Devices Are Discovered

Confirm that PipeWire and WirePlumber are running in the same user session as
the process. Devicelab supports WirePlumber only. Device discovery uses
GStreamer's PipeWire provider and does not shell out to `wpctl` or `pw-cli`.

An empty snapshot is valid. Check logs for unsupported caps or provider errors.

## The Configured Device Is Missing

Refresh discovery and use the exact `AudioDevice.id`, not `name` or a transient
PipeWire serial. Profile or local PipeWire rule changes can alter `node.name`.
There is deliberately no fallback to the current default device.

## `start()` Fails Although Construction Succeeded

Constructors validate deterministic configuration but do not probe devices,
open input files, or build media graphs. Device disappearance, an unreadable or
malformed file, unavailable plugins, and caps negotiation errors therefore
appear during `start()`. Inspect the raised `PipelineError.__cause__` and logs.

For speaker playback, some downstream negotiation can be deferred until the
first `submit()` because startup does not require priming audio.

## Capture Loses Audio Or Reports Discontinuity

Live microphone and real-time file paths intentionally use bounded `DROP_OLD`
queues. Keep handler callbacks short and move expensive work to an
application-owned bounded queue. Recording can also backpressure the graph.

`discontinuity=True` is expected after a drop, timestamp gap, malformed packet,
or microphone restart. Reset stateful application processing at that boundary
when necessary.

Use `FileReplayMode.AS_FAST_AS_POSSIBLE` when every valid file frame must reach
the handler.

## Stop Is Slow Or The Process Does Not Exit

Graceful stop drains accepted audio and finalizes recording, so it can take
longer than immediate stop. A capture callback must return cooperatively.
Python cannot terminate a handler stuck in arbitrary application code, and its
non-daemon delivery thread may delay process exit even after logical teardown.

Use `stop(immediate=True)` when discarding queued work is acceptable. This does
not make non-cooperative callback code cancellable.

## A Recording Path Is Rejected

Check all of the following:

- extension is `.wav` or `.flac`;
- the path contains a filename;
- the parent directory already exists;
- the target does not exist, or `overwrite=True` was explicitly supplied;
- `overwrite=True` is not used without `record_to`.

The library does not create parent directories. Every microphone recovery
segment has an independent collision check.

## A Recording Is Missing After Immediate Stop

This is expected for speaker playback. The output remains private until encoder
EOS; immediate stop removes the unpublished temporary file rather than
publishing incomplete data. Use graceful `stop()` to guarantee that accepted
speaker PCM is finalized and published.

## `submit()` Blocks

At ordinary audio rates this is bounded backpressure. AppSrc staging targets
approximately 250 ms of configured PCM after integer byte rounding. The call
resumes as downstream playback consumes data or fails/stops. Recording can add
downstream backpressure. At extremely low sample rates the rounded byte limit
can be zero and is treated by GStreamer as unlimited; avoid such formats when
bounded playback is required.

Do not hold application locks needed by stop or failure-handling code while
calling `submit()`.

## A Submission Is Partially Accepted

Catch `PlaybackSubmissionError` and inspect `accepted_frames`. Only that leading
prefix was accepted. Its `__cause__` distinguishes interruption by lifecycle
state from a retained media failure. Never resend the entire array blindly if
duplicate playback is unacceptable.

## Timeout Semantics Are Unexpected

- A `start()` or `stop()` timeout is terminal, retained in `failure`, and
  triggers best-effort forced teardown.
- A `wait()` timeout only ends that wait; the pipeline continues running.
- All finite timeouts must be positive and finite.
- Only `wait(timeout=None)` supports an unbounded wait.

After a terminal failure, repeated lifecycle calls can raise the same retained
failure. `state == STOPPED` does not by itself mean success; inspect `failure` or
allow `wait()`/`stop()` to report it.

## Hardware Tests Are Skipped

Set the stable IDs and use the opt-in recipe:

```shell
export LUMIVOX_DEVICELAB_MICROPHONE_ID='alsa_input.example'
export LUMIVOX_DEVICELAB_SPEAKER_ID='alsa_output.example'
just test_hardware
```

Each category skips independently if its variable is absent. Missing GStreamer
dependencies also produce a skip. The speaker test emits audible audio.
