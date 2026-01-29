# Решение проблем

[Оглавление](README.md) | [English version](../en/troubleshooting.md)

## `No module named gi`

Установите PyGObject, GStreamer introspection и gst-python через системный
package manager. Полностью изолированный virtualenv часто не видит эти bindings.
В checkout репозитория пересоздайте окружение:

```shell
just postclone
```

Команда использует `/usr/bin/python3 --system-site-packages`.

## Отсутствует GStreamer factory

Установите base, good и PipeWire plugins из раздела
[Установка](installation.md). Проверьте окружение:

```shell
just test_gstreamer
gst-inspect-1.0 pipewiresrc
gst-inspect-1.0 pipewiresink
```

Для WAV/FLAC дополнительно нужны соответствующие parser, decoder и encoder
factories.

## Устройства не обнаружены

Убедитесь, что PipeWire и WirePlumber работают в той же пользовательской сессии,
что и процесс. Devicelab поддерживает только WirePlumber. Discovery использует
PipeWire provider GStreamer и не запускает `wpctl` или `pw-cli`.

Пустой snapshot допустим. Проверьте логи на неподдерживаемые caps и ошибки
provider.

## Настроенное устройство отсутствует

Обновите discovery и используйте точный `AudioDevice.id`, а не `name` или
временный PipeWire serial. Изменение профиля или локальных правил может изменить
`node.name`. Перехода на текущее устройство по умолчанию намеренно нет.

## `start()` падает после успешного конструктора

Конструкторы проверяют детерминированную конфигурацию, но не опрашивают
устройства, не открывают входные файлы и не строят медиаграфы. Поэтому исчезшее
устройство, недоступный или поврежденный файл, отсутствующие plugins и ошибки
caps negotiation проявляются во время `start()`. Проверьте
`PipelineError.__cause__` и логи.

У speaker часть downstream negotiation может быть отложена до первого
`submit()`, поскольку startup не требует priming audio.

## Capture теряет звук или сообщает discontinuity

Live microphone и realtime file paths намеренно используют ограниченные очереди
`DROP_OLD`. Делайте callbacks handler короткими и переносите тяжелую работу в
собственную ограниченную очередь приложения. Запись также может создать
обратное давление.

`discontinuity=True` ожидаем после потери, timestamp gap, некорректного пакета
или restart микрофона. При необходимости сбрасывайте stateful обработку
приложения на этой границе.

Используйте `FileReplayMode.AS_FAST_AS_POSSIBLE`, если каждый корректный кадр
файла должен дойти до handler.

## Stop выполняется долго или процесс не завершается

Graceful stop завершает принятый звук и финализирует запись, поэтому может быть
медленнее immediate stop. Capture callback обязан возвращать управление. Python
не может завершить handler, зависший в произвольном коде приложения; его
non-daemon delivery thread может задержать завершение процесса даже после
логического teardown.

Используйте `stop(immediate=True)`, если очередь можно отбросить. Это не делает
некооперативный callback отменяемым.

## Путь записи отклонен

Проверьте все условия:

- расширение `.wav` или `.flac`;
- в пути есть имя файла;
- родительская директория уже существует;
- целевой файл не существует либо явно задан `overwrite=True`;
- `overwrite=True` не используется без `record_to`.

Библиотека не создает директории. Каждый сегмент восстановления микрофона
проверяет коллизию независимо.

## После immediate stop нет записи

Для speaker playback это ожидаемо. Результат остается приватным до encoder EOS;
immediate stop удаляет неопубликованный временный файл вместо публикации
неполных данных. Используйте graceful `stop()`, чтобы гарантировать финализацию
и публикацию принятого speaker PCM.

## `submit()` блокируется

На обычных звуковых частотах это ограниченное обратное давление. Staging AppSrc
рассчитан примерно на 250 ms PCM после целочисленного округления числа байтов.
Вызов продолжится, когда downstream воспроизведет данные либо завершится ошибкой
или остановкой. Запись может добавить downstream backpressure. На крайне низкой
частоте округленный byte limit может стать нулевым и трактоваться GStreamer как
неограниченный; избегайте таких форматов, если нужна bounded playback.

Не удерживайте во время `submit()` locks приложения, которые нужны обработчику
остановки или ошибки.

## Отправка принята частично

Перехватите `PlaybackSubmissionError` и проверьте `accepted_frames`. Был принят
только этот начальный префикс. `__cause__` различает lifecycle interruption и
сохраненную media failure. Не отправляйте весь массив повторно вслепую, если
дублирование звука недопустимо.

## Неожиданное поведение timeout

- Timeout `start()` или `stop()` терминален, сохраняется в `failure` и запускает
  best-effort forced teardown.
- Timeout `wait()` завершает только это ожидание; пайплайн продолжает работать.
- Любой конечный timeout должен быть положительным и конечным.
- Только `wait(timeout=None)` поддерживает неограниченное ожидание.

После терминальной ошибки повторные lifecycle calls могут выбросить ту же
сохраненную ошибку. `state == STOPPED` сам по себе не означает успех; проверяйте
`failure` или позвольте `wait()`/`stop()` сообщить ошибку.

## Hardware tests пропущены

Задайте стабильные IDs и используйте opt-in recipe:

```shell
export LUMIVOX_DEVICELAB_MICROPHONE_ID='alsa_input.example'
export LUMIVOX_DEVICELAB_SPEAKER_ID='alsa_output.example'
just test_hardware
```

Каждая категория независимо пропускается без своей переменной. Отсутствующие
зависимости GStreamer также приводят к skip. Speaker test воспроизводит слышимый
звук.
