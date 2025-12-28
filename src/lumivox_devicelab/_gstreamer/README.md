# Внутренний слой GStreamer

Этот пакет является внутренней границей реализации GStreamer в Devicelab. Его
обёртки над элементами помогают строить pipeline, но не входят в публичный API
и не являются builder-ами pipeline. Публичные capture и playback API не должны
возвращать или принимать объекты GStreamer.

## Инварианты

- Граница Python-аудио использует `RawAudioSpec`: interleaved PCM `S16LE`
  NumPy-массивы с явно заданными rate и числом каналов.
- `get_gst()` является единственной точкой загрузки PyGObject и GStreamer во
  время выполнения.
- Отсутствующие bindings или фабрики элементов выбрасывают внутренние ошибки из
  `runtime.py`; `_PipelineRuntime` преобразует runtime-, bus- и worker-сбои в
  публичные pipeline errors.
- `_PipelineGraph` владеет добавлением и связыванием элементов и освобождением
  request-pad. `_PipelineRuntime` отдельно владеет lifecycle, bus monitoring,
  worker threads и распространением ошибок.

## Документация

| Тема | Каноническое расположение |
| --- | --- |
| Аудиоформат, канальное отображение, конвертация и caps | `audio.py`, `elements/audio.md` |
| Границы Python-GStreamer и timestamps | `elements/app.py`, `elements/app.md` |
| Очереди, ветвление и время replay | `elements/flow.py`, `elements/flow.md` |
| Конфигурация PipeWire source и sink | `elements/pipewire.py`, `elements/pipewire.md` |
| WAV- и FLAC-примитивы | `elements/file.py`, `elements/file.md` |
| Graph ownership и lifecycle | `graph.py`, `pipeline_runtime.py`, `pipeline_runtime.md` |

## Границы тестов

Pure validation, graph cleanup и lifecycle races используют unit tests без
реального оборудования. Свойства элементов и контролируемые pipeline используют
GStreamer, когда он доступен. Hardware-тесты PipeWire остаются отдельными и
opt-in; recovery проверяется в capture-вехах.
