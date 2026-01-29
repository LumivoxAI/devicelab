# Руководство по разработке

[Оглавление](README.md) | [English version](../en/development.md)

## Настройка репозитория

Установите системные пакеты из раздела [Установка](installation.md), а также
[`uv`](https://docs.astral.sh/uv/) и
[`just`](https://github.com/casey/just). В корне репозитория выполните:

```shell
just postclone
```

Команда создает `.venv` из `/usr/bin/python3` с `--system-site-packages`, чтобы
оставались доступны установленные ОС bindings PyGObject и GStreamer. Затем она
синхронизирует все группы зависимостей и extras из `uv.lock`.

После обычных изменений зависимостей используйте `just sync`. Выполняйте
`just lock` только при намеренном обновлении зафиксированного lockfile.

## Структура исходников

```text
src/lumivox_devicelab/   публичные контракты и реализация
  _gstreamer/            внутренние runtime, графы, элементы и recovery
examples/                запускаемые примеры
tests/                   pure, контролируемые GStreamer и hardware tests
.agent/                  текущие контракты проекта и архитектуры
```

Перед изменением реализации прочитайте `.agent/project.md` и
`.agent/architecture.md`: они определяют scope и behavioral invariants.
`.agent/tasks/README.md` описывает процесс работы с активными task documents.

## Архитектурные границы

Публичный API ориентирован на сценарии. Не раскрывайте объекты GStreamer или
PipeWire и не создавайте абстрактную plugin-систему backend без реальной
необходимости. Текущие внутренние слои отвечают за:

- инициализацию GStreamer и проверенное создание элементов;
- сборку графа и владение request pads;
- lifecycle, bus monitoring, отмену, workers и ошибки;
- доставку захвата и восстановление микрофона;
- ветки записи и атомарную публикацию;
- PipeWire discovery и выбор source/sink.

Сохраняйте real-time контракт: все очереди live path ограничены, политика
переполнения всегда явная. Публичный звук остается interleaved NumPy PCM S16LE.
Логирующие объекты принимают явный `Logger` Lumivox Core и добавляют
`module="devicelab"`; реализация не должна настраивать логирование.

При изменении публичного API, потоков, timestamps, буферизации, ошибок, backend
boundaries или lifecycle в том же change обновляйте `.agent/project.md` или
`.agent/architecture.md`. При user-visible изменениях синхронно обновляйте
`doc/en` и `doc/ru`.

## Основные команды

Все recipes показывает `just`. Основные workflows:

| Команда | Назначение |
| --- | --- |
| `just postclone` | Создать и полностью синхронизировать dev environment |
| `just sync` | Синхронизировать project и development dependencies |
| `just lock` | Намеренно обновить `uv.lock` |
| `just lock_check` | Проверить lockfile без изменений |
| `just fmt` | Отформатировать репозиторий Ruff |
| `just fmt_check` | Проверить форматирование без изменений |
| `just lint` | Запустить lint checks Ruff |
| `just lint_fix` | Применить безопасные автоматические исправления Ruff |
| `just typecheck` | Запустить strict mypy |
| `just test` | Запустить tests; hardware остается opt-in |
| `just test_pure` | Запустить tests без GStreamer и оборудования |
| `just test_gstreamer` | Запустить контролируемые GStreamer tests без hardware |
| `just test_hardware` | Запустить opt-in tests реального PipeWire оборудования |
| `just test_all` | Тестировать со всеми dependency groups/extras, без hardware opt-in |
| `just build` | Пересобрать wheel и sdist без локальных uv sources |
| `just precommit` | Проверить format, lint, types, tests и lockfile |

`just precommit` является полной обязательной локальной проверкой. При изменении
публичных exports или package layout также выполните `just build` и проверьте
оба artifact на caches, prototypes и неподдерживаемые файлы.

## Уровни тестов

### Pure tests

```shell
just test_pure
```

Для lifecycle races используйте детерминированные fakes, для recovery windows
внедряйте monotonic time. Избегайте assertions на производительность по wall
time.

### Контролируемые GStreamer tests

```shell
just test_gstreamer
```

По возможности они используют контролируемые графы без PipeWire. При отсутствии
PyGObject, GStreamer 1.24+ или нужных factories тесты пропускаются с конкретной
причиной.

### Реальное PipeWire оборудование

Доступ к hardware включается явно и требует стабильных PipeWire `node.name`:

```shell
export LUMIVOX_DEVICELAB_MICROPHONE_ID='alsa_input.example'
export LUMIVOX_DEVICELAB_SPEAKER_ID='alsa_output.example'
just test_hardware
```

Без опции `--run-pipewire-hardware`, включенной в `just test_hardware`, hardware
tests пропускаются. Каждая категория устройств независимо пропускается без
соответствующей переменной. Speaker tests воспроизводят слышимый звук и требуют
активную пользовательскую сессию PipeWire/WirePlumber.

Pytest markers:

- `gstreamer`: контролируемый тест, требующий bindings и plugins;
- `pipewire_hardware(device)`: opt-in test для `microphone` или `speaker`.

## Добавление или изменение пайплайна

1. Определите конфигурацию и неизменяемые публичные данные без backend objects.
2. Проверяйте детерминированные аргументы в конструкторе; работу с устройством,
   файлом и GStreamer откладывайте до `start()`.
3. Задайте емкость очереди и явную политику overflow: block или drop old.
4. Задайте границы graceful и immediate shutdown.
5. Убедитесь, что асинхронные ошибки разблокируют публичные операции и сохраняют
   первую терминальную причину.
6. Закройте доступ к графу до его освобождения и не допускайте поздних вызовов
   GStreamer из workers.
7. Протестируйте успех, неверные состояния, отмену startup, timeout, worker
   failure, bus error, EOS, teardown races и повторный stop.
8. Обновите архитектуру и обе языковые версии документации.
9. Запустите `just precommit`; при изменении package layout или exports также
   запустите `just build`.

## Стиль и инструменты

- Минимальная версия синтаксиса: Python 3.11.
- Ruff: длина строки 120, двойные кавычки, lint families `E`, `F` и `I`.
- Mypy работает в strict mode для `src/lumivox_devicelab`, `tests` и `examples`.
- Пакет типизирован и включает `py.typed`.
- Публичные exports явно заданы в `lumivox_devicelab.__all__` и покрыты тестами.

Примеры не устанавливаются как entry points. Они должны запускаться напрямую из
checkout, а их путь `--help` не должен требовать работающее аудиооборудование.
