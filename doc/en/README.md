# Lumivox Devicelab Documentation

[Русская версия](../ru/README.md) | [Project README](../../README.md)

Lumivox Devicelab provides synchronous Python APIs for microphone capture,
speaker playback, and WAV/FLAC input on Linux. GStreamer performs media
processing, and PipeWire with WirePlumber provides device discovery and
routing.

## Documentation Map

1. [Installation](installation.md) describes supported systems, required OS
   packages, Python installation, and environment validation.
2. [Usage](usage.md) contains task-oriented examples for discovery, capture,
   playback, recording, channel selection, and shutdown.
3. [Pipelines](pipelines.md) explains each media graph, lifecycle, timestamps,
   recovery, buffering, and composition.
4. [Guarantees and limitations](guarantees.md) defines audio ownership,
   concurrency, callback threading, error, and real-time guarantees.
5. [Public API reference](api-reference.md) lists all supported public types,
   signatures, defaults, and error classes.
6. [Development](development.md) covers repository setup, architecture,
   testing, quality checks, and contribution rules.
7. [Troubleshooting](troubleshooting.md) covers common installation, device,
   lifecycle, recording, and latency problems.

## Scope At A Glance

Supported:

- immutable microphone and speaker discovery snapshots;
- live microphone capture through a synchronous handler;
- WAV and FLAC capture in real-time or as-fast-as-possible mode;
- synchronous, thread-safe speaker submissions with backpressure that is
  bounded at ordinary audio rates (see [limitations](guarantees.md));
- optional WAV or FLAC recording for microphones and speakers;
- automatic recovery from specific microphone-source failures.

Not currently supported:

- Windows or macOS;
- PulseAudio-only or non-WirePlumber session-manager configurations;
- video and arbitrary media processing;
- device hotplug subscriptions or default-device change notifications;
- automatic fallback when a configured device is unavailable;
- asyncio-native APIs, VAD, echo cancellation, speech recognition, or speech
  synthesis.

## Core Contract

All public audio buffers are interleaved NumPy PCM arrays with dtype
`numpy.dtype("<i2")`:

- mono shape: `(frames,)`;
- multichannel shape: `(frames, channels)`;
- sample rate and channel count: defined by `AudioFormat`;
- byte order and representation: signed 16-bit little-endian (`S16LE`).

Every object that produces logs requires an explicit Lumivox Core `Logger`.
Applications own logging configuration; this package neither configures logging
nor exposes GStreamer or PipeWire objects in its public API.
