# Lumivox Devicelab

[English](#overview) | [Русская документация](doc/ru/README.md) | [LLM reference](doc/llm/devicelab.md)

## Overview

Lumivox Devicelab is a Python library for low-latency audio capture and
playback in real-time voice applications. It provides scenario-oriented APIs
for PipeWire microphones and speakers through GStreamer, plus WAV/FLAC file
capture and optional recording.

The public audio format is always interleaved PCM `S16LE` stored in NumPy
arrays. Live capture and recording use bounded queues with documented overflow
or backpressure behavior; speaker buffering and its extreme-low-rate limitation
are documented explicitly. Pipeline lifecycle, callback threading, timestamps,
device recovery, and failures are part of the public contract.

## Supported Environment

- Linux, with Ubuntu 24.04 as the supported baseline
- Python 3.11 through 3.14
- GStreamer 1.24 or newer
- PipeWire 1.0 or newer
- WirePlumber 0.4.17 or newer
- Rolling Arch Linux on a best-effort basis

WirePlumber is the only supported PipeWire session manager. Devicelab does not
currently support other operating systems, media other than audio, or an
asyncio API.

## Quick Start

Install the required system audio stack first. See the full
[installation guide](doc/en/installation.md) for Ubuntu and Arch Linux package
commands and virtual-environment requirements.

Until a registry release is available, install the Python package from Git:

```shell
pip install "lumivox-devicelab @ git+https://github.com/LumivoxAI/devicelab.git@master"
```

Discover a microphone and retain its stable ID:

```python
from lumivox_core.logger import LoggingConfig, configure_logging, get_logger
from lumivox_devicelab import MicrophoneDeviceDiscovery

configure_logging(LoggingConfig(application="voice-agent"))
logger = get_logger(component="audio")

snapshot = MicrophoneDeviceDiscovery(logger=logger).snapshot()
for device in snapshot.devices:
    print(device.id, device.name, device.is_default)
```

Device IDs are PipeWire `node.name` values. They are stable within the current
PipeWire configuration, but profile or local rule changes can alter them.
Pipelines never silently fall back to a default device.

## Documentation

- [English documentation](doc/en/README.md)
- [Русская документация](doc/ru/README.md)
- [Самодостаточный справочник для LLM](doc/llm/devicelab.md)
- [Installation](doc/en/installation.md)
- [Usage guide](doc/en/usage.md)
- [Pipelines](doc/en/pipelines.md)
- [Guarantees and limitations](doc/en/guarantees.md)
- [Public API reference](doc/en/api-reference.md)
- [Development guide](doc/en/development.md)
- [Troubleshooting](doc/en/troubleshooting.md)

Runnable source examples are in [`examples/`](examples/). The complete local
verification command for contributors is `just precommit`.

## License

Apache License 2.0. See [LICENSE](LICENSE).
