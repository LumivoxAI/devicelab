# Pipeline Runtime

`_PipelineRuntime` и `_PipelineGraph` являются внутренними composition-компонентами.
Они не экспортируют GStreamer-объекты через публичный API.

## Lifecycle

Runtime реализует single-use последовательность
`created -> starting -> running -> stopping -> stopped`. `start()` завершается
только после сообщения `PLAYING` от принадлежащего graph pipeline и успешного
readiness hook. `stop()` отменяет работу и ожидает teardown в пределах общего
monotonic deadline; `wait()` только наблюдает terminal completion и не меняет
состояние при timeout.

Control thread выполняет teardown в следующем порядке:

1. Устанавливает cancellation signal.
2. Закрывает graph для новых операций и переводит pipeline в `NULL`, чтобы
   разблокировать уже начатые media-вызовы.
3. Ограниченно ожидает supervised workers.
4. Освобождает request pads и элементы.
5. Публикует `stopped` и сохранённый failure.

Worker, вызвавший `stop()`, только отправляет запрос control thread и не ожидает
самого себя. Все workers non-daemon. Если callback не возвращается, timeout всё
равно закрывает graph и публикует logical `stopped`; поток остаётся жив до
возврата callback.

## Errors And Access

Первый fatal `PipelineError` остаётся authoritative. Последующие ошибки teardown
добавляются в `secondary_errors`. Bus warning только логируется; bus error и
worker exception являются fatal, а EOS использует pipeline-specific hook или
нормальную остановку по умолчанию.

Worker получает `_WorkerContext` с cancellation API и `use_graph()`. Graph
выполняет такие операции под lock и после начала release отклоняет новые вызовы,
поэтому поздний worker не может снова войти в GStreamer.
