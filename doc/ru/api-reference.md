# Справочник публичного API

[Оглавление](README.md) | [English version](../en/api-reference.md)

Все поддерживаемые имена импортируются из `lumivox_devicelab`. Аргументы после
`*` являются keyword-only. Публичные dataclasses конфигурации и discovery
являются frozen и slotted.

## Конфигурация аудио

### `AudioFormat`

```python
AudioFormat(sample_rate: int, channels: int)
```

Задает точную нормализованную частоту и число каналов. Оба значения -
положительные `int` не больше `2**31 - 1`; `bool` запрещен. Публичное хранение
всегда использует PCM S16LE независимо от нативного формата устройства.

### `ChannelSelection`

```python
ChannelSelection(source_channels: int, mapping: tuple[int, ...])
```

Выбор входных каналов только для микрофона. `source_channels` положителен.
`mapping` копируется в неизменяемый непустой tuple с индексами в диапазоне
`[0, source_channels)`. Повторы разрешены. Микрофонный пайплайн требует
`len(mapping) == audio_format.channels`.

## Handler и данные захвата

### `CaptureContext`

```python
CaptureContext(
    audio_format: AudioFormat,
    generation: int,
    wall_time_anchor_ns: int,
    running_time_anchor_ns: int,
)
```

Описывает одно поколение захвата. Числовые поля - неотрицательные `int`.
Начальный `generation` равен нулю, успешная замена микрофонного графа увеличивает
его. Два anchor задают преобразование running time в wall-clock time.

### `CapturedChunk`

```python
CapturedChunk(
    samples: numpy.ndarray,
    generation: int,
    captured_at_ns: int,
    running_time_ns: int,
    discontinuity: bool,
)
```

Содержит хотя бы один кадр PCM S16LE. Конструктор делает независимую доступную
для записи копию `samples`. Время и generation неотрицательны.
`discontinuity` - строго `bool`, предупреждающий, что нельзя предполагать
непрерывность с предыдущим выданным chunk.

### `CaptureHandler`

```python
class CaptureHandler:
    def on_start(self, context: CaptureContext) -> None: ...
    def on_chunk(self, chunk: CapturedChunk) -> None: ...
    def on_restart(self, context: CaptureContext, cause: PipelineError) -> None: ...
    def on_stop(self, context: CaptureContext, cause: PipelineError | None) -> None: ...
```

`on_chunk()` абстрактен, остальные методы по умолчанию ничего не делают. Один
delivery worker вызывает их последовательно по жизненному циклу.
`on_restart()` относится к микрофону. Исключение завершает пайплайн ошибкой.
Если `on_start()` не завершился успешно, `on_stop()` не гарантируется.

## Обнаружение устройств

### `PcmSampleKind`

`StrEnum` со значениями `SIGNED_INTEGER = "signed_integer"`,
`UNSIGNED_INTEGER = "unsigned_integer"` и `FLOAT = "float"`.

### `ByteOrder`

`StrEnum` со значениями `LITTLE = "little"`, `BIG = "big"` и
`NOT_APPLICABLE = "not_applicable"`.

### `PcmFormat`

```python
PcmFormat(
    kind: PcmSampleKind,
    significant_bits: int,
    storage_bits: int,
    byte_order: ByteOrder,
)
```

Описывает нативный формат capability. Нужны именно enum instances. Число битов
положительно, `storage_bits >= significant_bits`.

### `IntRange`

```python
IntRange(minimum: int, maximum: int, step: int = 1)
```

Включительный диапазон положительных целых с шагом. `maximum >= minimum`, шаг
должен точно достигать `maximum` из `minimum`.

### `AudioCapability`

```python
AudioCapability(
    format: PcmFormat,
    sample_rates: tuple[int | IntRange, ...],
    channel_counts: tuple[int | IntRange, ...],
)
```

Сохраняет связь между одним PCM format и допустимыми частотами и числами
каналов. Обе коллекции копируются в непустые tuples.

### `AudioDevice`

```python
AudioDevice(
    id: str,
    name: str,
    is_default: bool,
    capabilities: tuple[AudioCapability, ...],
)
```

`id` и `name` непустые. Capabilities копируются в tuple и могут быть пустыми,
если ни одни caps нельзя представить. `id` основан на PipeWire `node.name`,
`name` является отображаемым описанием.

### `DeviceSnapshot`

```python
DeviceSnapshot(
    devices: tuple[AudioDevice, ...],
    default: AudioDevice | None,
)
```

Неизменяемая моментальная коллекция. Discovery гарантирует уникальные ID и не
более одного default. Пустой снимок и снимок без default допустимы.

### Discovery clients

```python
MicrophoneDeviceDiscovery(*, logger: Logger)
SpeakerDeviceDiscovery(*, logger: Logger)

snapshot() -> DeviceSnapshot
```

Каждый `snapshot()` синхронно запускает GStreamer PipeWire provider, собирает
source или sink devices и останавливает provider. При ошибках provider или
несогласованных metadata выбрасывается `DeviceError`. Live subscription нет.

## Жизненный цикл пайплайна

### `PipelineState`

`StrEnum` со значениями `CREATED`, `STARTING`, `RUNNING`, `STOPPING` и `STOPPED`,
строковые значения записаны в нижнем регистре.

Каждый публичный пайплайн имеет:

```python
@property
state -> PipelineState

@property
failure -> PipelineError | None

start(*, timeout: float = 10.0) -> None
stop(*, immediate: bool = False, timeout: float = 10.0) -> None
wait(*, timeout: float | None = None) -> None
```

Timeout должен быть конечным положительным real number, кроме `bool`. Только
`wait(timeout=None)` допускает неограниченное ожидание.

## Классы пайплайнов

### `MicrophoneCapturePipeline`

```python
MicrophoneCapturePipeline(
    *,
    logger: Logger,
    handler: CaptureHandler,
    audio_format: AudioFormat,
    device_id: str,
    channel_selection: ChannelSelection | None = None,
    record_to: str | os.PathLike[str] | None = None,
    overwrite: bool = False,
)
```

Захватывает один выбранный PipeWire microphone. `device_id` должен быть
непустым. Запись поддерживает WAV и FLAC. `overwrite=True` требует `record_to`.
Поиск устройства и построение графа отложены до `start()`.

### `FileReplayMode`

`StrEnum` со значениями `REALTIME = "realtime"` и
`AS_FAST_AS_POSSIBLE = "as_fast_as_possible"`. Передавайте enum member, а не
его строку.

### `FileCapturePipeline`

```python
FileCapturePipeline(
    *,
    logger: Logger,
    handler: CaptureHandler,
    audio_format: AudioFormat,
    path: str | os.PathLike[str],
    replay_mode: FileReplayMode,
)
```

Читает WAV или FLAC, выбранный по расширению пути. Доступ к файлу отложен до
`start()`. У файлового захвата нет `record_to`, выбора каналов и восстановления.

### `SpeakerPlaybackPipeline`

```python
SpeakerPlaybackPipeline(
    *,
    logger: Logger,
    audio_format: AudioFormat,
    device_id: str,
    record_to: str | os.PathLike[str] | None = None,
    overwrite: bool = False,
)

submit(data: numpy.ndarray) -> None
```

Воспроизводит PCM через один выбранный PipeWire speaker. `submit()` проверяет
точные dtype и shape и при необходимости блокируется на ограниченном staging.
Метод допустим только в состоянии running. На крайне низких частотах
целочисленно округленный byte limit AppSrc может стать нулевым, поэтому bounded
staging для таких форматов не гарантируется. Возврат означает прием данных
AppSrc, но не завершение физического воспроизведения. У `wait()` нет
естественной границы завершения speaker; для drain вызовите `stop()`. Запись
поддерживает WAV и FLAC.

## Иерархия ошибок

```text
Exception
└── DevicelabError
    ├── DeviceError
    │   └── DeviceNotFoundError
    └── PipelineError
        ├── PipelineStateError
        ├── PipelineTimeoutError (также TimeoutError)
        └── PlaybackSubmissionError
```

`DevicelabError` является общей базой публичных ошибок устройств и пайплайнов.
Обычная проверка аргументов по-прежнему выбрасывает стандартные `TypeError` или
`ValueError`.

### `PipelineError`

```python
PipelineError(message: str, *, cause: BaseException | None = None)
```

Причина доступна через стандартный `__cause__`. `secondary_errors` возвращает
tuple snapshot последующих ошибок, не заменивших основную.

### `PipelineStateError`

Возникает при недопустимой lifecycle-операции: например, повторном запуске или
ожидании из `created`.

### `PipelineTimeoutError`

Также наследует `TimeoutError`. Ошибки timeout запуска и остановки сохраняются;
timeout ожидания относится только к конкретному вызову wait.

### `PlaybackSubmissionError`

```python
PlaybackSubmissionError(
    message: str,
    *,
    accepted_frames: int,
    cause: BaseException | None = None,
)
```

Сообщает число начальных кадров разделенной отправки, успешно принятых до
прерывания.

### Ошибки устройств

`DeviceError` представляет ошибки discovery или metadata.
`DeviceNotFoundError` означает, что ID устройства не разрешен. Если это
произошло внутри startup пайплайна, она обычно доступна как cause сохраненной
`PipelineError`.

## Проверка целых значений

Если поле явно не описано как неотрицательное, целочисленные поля перечисленных
configuration и discovery value types требуют значения Python `int` в диапазоне
`[1, 2**31 - 1]`; `bool` запрещен. То же правило применяется к отдельным
значениям sample rate и channel count внутри `AudioCapability`.
