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
- Отсутствующие bindings или фабрики элементов выбрасывают domain errors из
  `runtime.py`; ошибки bus pipeline и lifecycle recovery относятся к будущему
  слою pipeline.
- Builder владеет добавлением и связыванием элементов, освобождением
  request-pad, состоянием pipeline, worker threads и распространением ошибок.

## Документация

| Тема | Каноническое расположение |
| --- | --- |
| Аудиоформат, канальное отображение, конвертация и caps | `audio.py`, `elements/audio.md` |
| Границы Python-GStreamer и timestamps | `elements/app.py`, `elements/app.md` |
| Очереди, ветвление и время replay | `elements/flow.py`, `elements/flow.md` |
| Конфигурация PipeWire source и sink | `elements/pipewire.py`, `elements/pipewire.md` |
| WAV- и FLAC-примитивы | `elements/file.py`, `elements/file.md` |

## Границы тестов

Pure validation и преобразование буферов используют unit tests без реального
оборудования. Свойства элементов могут использовать GStreamer, когда он
доступен. Контролируемые полные pipeline, hardware-тесты PipeWire, поведение
lifecycle и recovery относятся к capture- и playback-вехам.
