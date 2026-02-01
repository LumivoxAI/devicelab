# Installation

[Documentation index](README.md) | [Русская версия](../ru/installation.md)

## Requirements

The supported baseline is Ubuntu 24.04 with Python 3.11-3.14, GStreamer 1.24+,
PipeWire 1.0+, and WirePlumber 0.4.17+. Rolling Arch Linux is tested on a
best-effort basis. Other operating systems and PipeWire session managers are
not supported by the current release.

PyGObject, GStreamer introspection data and plugins, PipeWire, and WirePlumber
are system dependencies. `pip` and `uv` do not install them.

## Ubuntu 24.04

```shell
sudo apt update
sudo apt install \
  python3-gi python3-gst-1.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-pipewire pipewire wireplumber
```

## Arch Linux

```shell
sudo pacman -S \
  python-gobject gst-python gstreamer gst-plugins-base gst-plugins-good \
  gst-plugin-pipewire pipewire wireplumber
```

Arch Linux support is best effort because its rolling package versions can be
newer than those covered by the supported baseline.

## Install The Python Package

Until a package-registry release is available:

```shell
pip install "lumivox-devicelab @ git+https://github.com/LumivoxAI/devicelab.git@master"
```

The package also obtains Lumivox Core from its `master` branch. Pin Git commit
references in deployments that require reproducible dependencies.

If the Python interpreter in a virtual environment cannot import `gi`, create
the environment from the distribution Python with access to system packages.
For a repository checkout, the supported setup command handles this:

```shell
just postclone
```

Its equivalent core commands are:

```shell
uv venv --python /usr/bin/python3 --system-site-packages
uv sync --python /usr/bin/python3 --all-groups --all-extras
```

## Validate The Environment

Check the bindings and GStreamer version:

```shell
uv run python -c 'import gi; gi.require_version("Gst", "1.0"); from gi.repository import Gst; Gst.init(None); print(Gst.version_string())'
```

Run controlled tests, which report unavailable bindings or plugins as skips
with an actionable reason:

```shell
just test_gstreamer
```

The runtime paths require factories provided by the installed plugin packages,
including `appsrc`, `appsink`, `audioconvert`, `audioresample`, `volume`, `queue`,
`clocksync`, `tee`, `filesink`, `pipewiresrc`, `pipewiresink`, `wavparse`,
`wavenc`, `flacparse`, `flacdec`, and `flacenc`.

## Logging Setup

Discovery and pipeline objects require an explicit Lumivox Core logger:

```python
from lumivox_core.logger import LoggingConfig, configure_logging, get_logger

configure_logging(LoggingConfig(application="voice-agent"))
logger = get_logger(component="audio")
```

Configure logging once in the application. Devicelab binds its own
`module="devicelab"` context and never configures logging implicitly.
