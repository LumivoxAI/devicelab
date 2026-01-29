# Руководство по использованию

[Оглавление](README.md) | [English version](../en/usage.md)

Во всех примерах предполагается, что `logger` создан согласно разделу
[Установка](installation.md).

## Обнаружение устройств

Обнаружение выполняется синхронно и возвращает неизменяемый снимок текущего
состояния:

```python
from lumivox_devicelab import MicrophoneDeviceDiscovery, SpeakerDeviceDiscovery

microphones = MicrophoneDeviceDiscovery(logger=logger).snapshot()
speakers = SpeakerDeviceDiscovery(logger=logger).snapshot()

for device in microphones.devices:
    marker = " (default)" if device.is_default else ""
    print(f"{device.id}: {device.name}{marker}")
```

При создании пайплайна используйте `device.id`, а не отображаемое имя. ID
основан на PipeWire `node.name`: обычно он стабилен, но может измениться после
смены профиля или локальных правил. Во время `start()` пайплайн заново разрешает
ID через свежий снимок. Отсутствующий ID вызывает явную ошибку, без неявного
выбора устройства по умолчанию.

Снимок не обновляется при подключении устройств. Для обновления снова вызовите
`snapshot()`.

## Обработка захваченного звука

Унаследуйте `CaptureHandler`; реализовать обязательно только `on_chunk()`:

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

Все callbacks последовательно вызывает один выделенный delivery worker. Не
блокируйте его на неограниченное время. Копировать `chunk.samples` для владения
не нужно: каждый chunk уже владеет отдельным доступным для записи массивом
NumPy. Для тяжелой обработки передавайте chunk в собственную ограниченную
очередь приложения.

Проверяйте `chunk.discontinuity`, прежде чем считать chunk продолжением
предыдущего. Флаг устанавливается после потерь, некорректных пакетов, разрывов
времени и восстановления микрофона.

## Захват с микрофона

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

Конструктор проверяет аргументы, но не обращается к устройству. Runtime-настройка
начинается в `start()`. Микрофонный пайплайн повторяет попытку после определенных
ошибок источника, сохраняя публичное состояние `running`; подробности описаны в
разделе [Пайплайны](pipelines.md).

## Выбор входных каналов

`ChannelSelection` доступен только для микрофона. Индекс mapping соответствует
выходному каналу, а значение указывает номер входного канала:

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

Длина mapping должна совпадать с числом выходных каналов. Индексы источника
могут повторяться: `(0, 0)` дублирует нулевой канал в stereo. Согласование
завершится ошибкой, если источник не предоставляет ровно `source_channels`.
Без явного выбора применяется стандартное преобразование каналов GStreamer.

## Захват из WAV или FLAC

```python
from lumivox_devicelab import AudioFormat, FileCapturePipeline, FileReplayMode

pipeline = FileCapturePipeline(
    logger=logger,
    handler=AudioHandler(),
    audio_format=AudioFormat(sample_rate=16_000, channels=1),
    path="input.flac",
    replay_mode=FileReplayMode.AS_FAST_AS_POSSIBLE,
)
pipeline.start()
pipeline.wait()
```

`REALTIME` выдает данные по медиачасам. `AS_FAST_AS_POSSIBLE` использует
ограниченную блокирующую передачу без намеренной потери кадров. Естественный
конец файла, включая корректный пустой файл, завершает работу нормально.
Поддерживаются только WAV и FLAC, формат выбирается по расширению без учета
регистра.

## Воспроизведение

```python
import numpy as np

from lumivox_devicelab import AudioFormat, SpeakerPlaybackPipeline

audio_format = AudioFormat(sample_rate=16_000, channels=1)
pipeline = SpeakerPlaybackPipeline(
    logger=logger,
    audio_format=audio_format,
    device_id="alsa_output.example",
)

pipeline.start()
pipeline.submit(np.zeros(16_000, dtype="<i2"))
pipeline.stop()
```

`submit()` принимает точный S16LE-массив NumPy, соответствующий `AudioFormat`:
mono имеет форму `(frames,)`, многоканальный звук - `(frames, channels)`.
Пустые массивы запрещены. Не изменяйте входной массив до возврата `submit()`.

`submit()` можно вызывать из нескольких потоков приложения. Вызовы целиком
сериализуются, но порядок потоков, ожидающих lock, не обязан совпадать с
порядком входа в метод. При заполнении ограниченной staging queue вызывающий
поток блокируется; принятый звук намеренно не отбрасывается.

Возврат `submit()` означает, что AppSrc принял весь массив, но не что оборудование
закончило его воспроизведение. У speaker нет естественного события завершения:
`wait()` вернется только после `stop()` из другого потока или после ошибки.
Вызовите graceful `stop()`, чтобы отправить EOS и завершить принятый звук.

Если немедленная остановка или медиаошибка прервала большую отправку, проверьте
принятый префикс:

```python
from lumivox_devicelab import PlaybackSubmissionError

try:
    pipeline.submit(samples)
except PlaybackSubmissionError as error:
    print("accepted frames:", error.accepted_frames)
    print("cause:", error.__cause__)
```

## Запись захвата или воспроизведения

Микрофонный и speaker-пайплайны принимают путь `.wav` или `.flac`:

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

Родительская директория должна существовать. Итоговый файл атомарно публикуется
только после завершения encoder. Существующий файл запрещен без
`overwrite=True`. После восстановления микрофона создаются `capture.1.wav`,
`capture.2.wav` и далее. `FileCapturePipeline` запись не поддерживает.

Ветка записи ограничена и блокируется при заполнении. Это защищает полноту
файла, но может создать обратное давление и увеличить задержку захвата или
воспроизведения.

## Правильная остановка и ожидание

Все пайплайны одноразовые:

```text
created -> starting -> running -> stopping -> stopped
```

- `stop()` по умолчанию корректно завершает уже принятую работу в пределах
  timeout.
- `stop(immediate=True)` сразу инициирует отмену и может отбросить очередь.
- `wait()` ожидает завершения; timeout в `wait(timeout=...)` не останавливает
  пайплайн.
- Timeout по умолчанию для `start()` и `stop()` равен 10 секундам.
- Timeout запуска или остановки терминален; timeout ожидания влияет только на
  текущий вызов.
- Остановленный объект нельзя запустить повторно. Создайте новый пайплайн.

Если capture startup завершился до успешного возврата `on_start()`, в том числе
из-за исключения в самом `on_start()`, вызов `on_stop()` не гарантируется.
Освобождайте ресурсы, созданные до успешного запуска, в обработчике ошибки
`start()` вызывающей стороны.

Используйте `pipeline.state` и `pipeline.failure` для наблюдения. Методы
жизненного цикла повторно выбрасывают сохраненную ошибку пайплайна, поэтому не
полагайтесь только на опрос `state`.

## Примеры из репозитория

Примеры являются исходными файлами и не устанавливаются как console commands:

```shell
uv run python examples/microphone_capture.py
uv run python examples/microphone_capture.py 'alsa_input.example'
uv run python examples/file_capture.py input.wav
uv run python examples/file_capture.py --realtime input.flac
uv run python examples/speaker_playback.py 'alsa_output.example'
uv run python examples/recording.py 'alsa_input.example' output.wav
uv run python examples/record_playback_gui.py
```

Для любого скрипта доступен `--help`. Локальный NiceGUI demo по умолчанию
слушает только `127.0.0.1:8080` и требует зависимости для разработки.
