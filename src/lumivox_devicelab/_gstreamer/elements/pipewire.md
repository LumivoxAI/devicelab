# Элементы PipeWire GStreamer

Этот модуль содержит внутренние обёртки над `pipewiresrc` и `pipewiresink`. Они создают GStreamer-элемент сразу и не являются публичным API Devicelab. Если фабрика соответствующего плагина недоступна, создание завершается `GStreamerElementError`; ошибки подключения, согласования caps и устройства поступают через bus pipeline при запуске.

Обе обёртки принимают необязательные `target_object`, `client_name` и `name`. Пустые строки для `target_object` и `client_name` отклоняются с `ValueError`. `target_object` передаётся свойству PipeWire `target-object` и выбирает node по имени или serial; отсутствие значения оставляет выбор устройству и session manager. `client_name` задаёт имя GStreamer-клиента для PipeWire. `name` является необязательным именем GStreamer-элемента.

## PipeWireSrc

`PipeWireSrc` является обёрткой над `pipewiresrc` для захвата живого аудио из PipeWire. Полученный поток ещё не соответствует контракту Devicelab: последующие `AudioConvert`, `AudioResample` и `CapsFilter` должны нормализовать его к interleaved PCM `S16LE` с требуемыми частотой дискретизации и числом каналов.

| Свойство | Значение | Дефолт GStreamer | Политика |
| --- | --- | --- | --- |
| `do-timestamp` | `True` | `False` | Проставлять временные метки при выходе буфера из источника. |
| `use-bufferpool` | `False` | Зависит от типа потока | Не удерживать PipeWire buffer pool на стороне GStreamer; буферизация live-пути управляется явно. |
| `min-buffers` | `2` по умолчанию | `1` | Нижняя граница числа буферов, согласуемых с PipeWire. |
| `max-buffers` | `4` по умолчанию | Не ограничено | Верхняя граница числа буферов, согласуемых с PipeWire. |
| `stream-properties` | `media.type=Audio`, `media.category=Capture`, `media.role=Communication` | Не задано | Передать WirePlumber метаданные для маршрутизации и приоритетов коммуникационного capture-потока. |

Конструктор требует положительный `min_buffers`; `max_buffers` не может быть меньше `min_buffers`. Эти значения ограничивают только буферы, согласуемые между источником и PipeWire. Они не заменяют ограниченную leaky-очередь, обязательную далее на capture audio path.

## PipeWireSink

`PipeWireSink` является обёрткой над `pipewiresink` для вывода аудио в PipeWire. Перед ним playback pipeline должен привести поток к контракту Devicelab: interleaved PCM `S16LE` с явно заданными rate и channels.

| Свойство | Значение | Дефолт GStreamer | Политика |
| --- | --- | --- | --- |
| `sync` | `True` | `True` | Явно синхронизировать вывод с clock pipeline и PTS входных буферов. |
| `stream-properties` | `media.type=Audio`, `media.category=Playback`, `media.role=Communication` | Не задано | Передать WirePlumber метаданные для маршрутизации и приоритетов коммуникационного playback-потока. |

`sync=True` закреплён явно, хотя совпадает с текущим дефолтом GStreamer: задержка рендеринга определяется PTS и clock pipeline, а не скоростью, с которой upstream доставляет буферы. Поэтому `AppSource` формирует непрерывные PTS и duration из числа переданных frames. Параметр не гарантирует фиксированную end-to-end задержку: её также определяют PipeWire, согласование устройства и ограниченные очереди pipeline.
