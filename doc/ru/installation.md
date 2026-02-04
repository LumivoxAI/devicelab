# Установка

[Оглавление](README.md) | [English version](../en/installation.md)

## Требования

Базовая поддерживаемая система: Ubuntu 24.04, Python 3.13-3.14,
GStreamer 1.24+, PipeWire 1.0+ и WirePlumber 0.4.17+. Rolling Arch Linux
тестируется по мере возможности. Другие операционные системы и менеджеры
сессий PipeWire текущей версией не поддерживаются.

PyGObject, introspection-данные и плагины GStreamer, PipeWire и WirePlumber
являются системными зависимостями. `pip` и `uv` их не устанавливают.

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

Поддержка Arch Linux предоставляется по мере возможности: версии пакетов в
rolling release могут быть новее проверенной базовой конфигурации.

## Установка Python-пакета

До публикации в реестре пакетов устанавливайте библиотеку из Git:

```shell
pip install "lumivox-devicelab @ git+https://github.com/LumivoxAI/devicelab.git@master"
```

Lumivox Core также загружается из ветки `master`. Для воспроизводимых развертываний
закрепляйте зависимости по конкретным Git commit.

Если интерпретатор внутри virtualenv не импортирует `gi`, создайте окружение на
основе системного Python с доступом к системным пакетам. Для клона репозитория
это делает команда:

```shell
just postclone
```

Основные выполняемые ею команды:

```shell
uv venv --python /usr/bin/python3 --system-site-packages
uv sync --python /usr/bin/python3 --all-groups --all-extras
```

## Проверка окружения

Проверьте bindings и версию GStreamer:

```shell
uv run python -c 'import gi; gi.require_version("Gst", "1.0"); from gi.repository import Gst; Gst.init(None); print(Gst.version_string())'
```

Контролируемые тесты сообщают о недоступных bindings или плагинах как о
пропусках с указанием причины:

```shell
just test_gstreamer
```

Рабочим пайплайнам нужны фабрики из установленных пакетов плагинов, включая
`appsrc`, `appsink`, `audioconvert`, `audioresample`, `volume`, `queue`, `clocksync`,
`tee`, `filesink`, `pipewiresrc`, `pipewiresink`, `wavparse`, `wavenc`,
`flacparse`, `flacdec` и `flacenc`.

## Настройка логирования

Объектам обнаружения устройств и пайплайнам нужен явный logger Lumivox Core:

```python
from lumivox_core.logger import LoggingConfig, configure_logging, get_logger

configure_logging(LoggingConfig(application="voice-agent"))
logger = get_logger(component="audio")
```

Настраивайте логирование один раз в приложении. Devicelab добавляет контекст
`module="devicelab"` и никогда не настраивает логирование неявно.
