from __future__ import annotations

from types import SimpleNamespace
from threading import Event, Thread
from unittest.mock import Mock, call

import pytest

from lumivox_devicelab._gstreamer import graph as graph_module
from lumivox_devicelab._gstreamer.graph import _PipelineGraph
from lumivox_devicelab._gstreamer.runtime import GStreamerElementError
from lumivox_devicelab._gstreamer.elements.base import BaseElement
from lumivox_devicelab._gstreamer.elements.flow import Tee


class FakePad:
    def __init__(self, link_result: int = 0) -> None:
        self.link_result = link_result
        self.linked_to: object | None = None

    def link(self, sink: object) -> int:
        self.linked_to = sink
        return self.link_result


class FakeElement:
    def __init__(self, name: str) -> None:
        self.name = name
        self.links: list[FakeElement] = []
        self.link_result = True
        self.sink_pad: object | None = object()
        self.requested_pad: FakePad | None = None
        self.release_fails = False
        self.released_pads: list[FakePad] = []

    def link(self, next_element: FakeElement) -> bool:
        self.links.append(next_element)
        return self.link_result

    def get_pad_template(self, name: str) -> object | None:
        assert name == "src_%u"
        return object()

    def request_pad(self, template: object, name: None, caps: None) -> FakePad | None:
        del template, name, caps
        self.requested_pad = FakePad()
        return self.requested_pad

    def get_static_pad(self, name: str) -> object | None:
        assert name == "sink"
        return self.sink_pad

    def release_request_pad(self, pad: FakePad) -> None:
        if self.release_fails:
            raise RuntimeError("pad release failed")
        self.released_pads.append(pad)


class FakePipeline:
    def __init__(self) -> None:
        self.added: list[FakeElement] = []
        self.removed: list[FakeElement] = []
        self.add_failures: set[str] = set()
        self.remove_failures: set[str] = set()
        self.state_calls: list[object] = []
        self.state_result = 1
        self.null_requested = Event()

    def add(self, element: FakeElement) -> bool:
        if element.name in self.add_failures:
            return False
        self.added.append(element)
        return True

    def remove(self, element: FakeElement) -> bool:
        if element.name in self.remove_failures:
            return False
        self.removed.append(element)
        return True

    def set_state(self, state: object) -> int:
        self.state_calls.append(state)
        self.null_requested.set()
        return self.state_result


def _install_gst(monkeypatch: pytest.MonkeyPatch, pipeline: FakePipeline | None) -> SimpleNamespace:
    gst = SimpleNamespace(
        Pipeline=SimpleNamespace(new=lambda name: pipeline),
        PadLinkReturn=SimpleNamespace(OK=0),
        State=SimpleNamespace(NULL=object()),
        StateChangeReturn=SimpleNamespace(FAILURE=-1),
    )
    monkeypatch.setattr(graph_module, "get_gst", lambda: gst)
    return gst


def _element(impl: FakeElement) -> BaseElement:
    element = object.__new__(BaseElement)
    element._factory = impl.name
    element._name = impl.name
    element._impl = impl
    return element


def _tee(impl: FakeElement) -> Tee:
    tee = object.__new__(Tee)
    tee._factory = impl.name
    tee._name = impl.name
    tee._impl = impl
    return tee


def test_graph_owns_pipeline_membership_links_branches_and_release(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = FakePipeline()
    gst = _install_gst(monkeypatch, pipeline)
    logger = Mock()
    logger.bind.return_value = logger
    source_impl = FakeElement("source")
    tee_impl = FakeElement("tee")
    queue_impl = FakeElement("queue")
    sink_impl = FakeElement("sink")
    source = _element(source_impl)
    tee = _tee(tee_impl)
    queue = _element(queue_impl)
    sink = _element(sink_impl)

    graph = _PipelineGraph(logger=logger, name="capture")
    graph.add(source, tee, queue, sink)

    assert graph.use(lambda owned: owned) is pipeline
    assert graph.link(source, tee) is tee
    assert graph.branch(tee, queue, sink) is sink
    assert source_impl.links == [tee_impl]
    assert tee_impl.requested_pad is not None
    assert tee_impl.requested_pad.linked_to is queue_impl.sink_pad
    assert queue_impl.links == [sink_impl]

    assert graph.release() == ()
    assert graph.released
    assert pipeline.state_calls == [gst.State.NULL]
    assert tee_impl.released_pads == [tee_impl.requested_pad]
    assert pipeline.removed == [sink_impl, queue_impl, tee_impl, source_impl]
    assert graph.release() == ()
    assert pipeline.state_calls == [gst.State.NULL]
    logger.bind.assert_called_once_with(module="devicelab")


def test_partial_build_cleanup_releases_requested_pad_and_added_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = FakePipeline()
    _install_gst(monkeypatch, pipeline)
    tee_impl = FakeElement("tee")
    queue_impl = FakeElement("queue")
    sink_impl = FakeElement("sink")
    queue_impl.link_result = False
    tee = _tee(tee_impl)
    queue = _element(queue_impl)
    sink = _element(sink_impl)
    graph = _PipelineGraph(logger=Mock())
    graph.add(tee, queue, sink)

    with pytest.raises(GStreamerElementError, match="queue:queue -> sink:sink"):
        graph.branch(tee, queue, sink)

    assert graph.release() == ()
    assert tee_impl.released_pads == [tee_impl.requested_pad]
    assert pipeline.removed == [sink_impl, queue_impl, tee_impl]


def test_partial_add_cleanup_removes_successful_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = FakePipeline()
    pipeline.add_failures.add("sink")
    _install_gst(monkeypatch, pipeline)
    source_impl = FakeElement("source")
    sink_impl = FakeElement("sink")
    graph = _PipelineGraph(logger=Mock())

    with pytest.raises(GStreamerElementError, match="add sink:sink"):
        graph.add(_element(source_impl), _element(sink_impl))

    assert graph.release() == ()
    assert pipeline.removed == [source_impl]


def test_release_continues_and_retries_only_failed_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = FakePipeline()
    _install_gst(monkeypatch, pipeline)
    logger = Mock()
    logger.bind.return_value = logger
    source_impl = FakeElement("source")
    tee_impl = FakeElement("tee")
    queue_impl = FakeElement("queue")
    source = _element(source_impl)
    tee = _tee(tee_impl)
    queue = _element(queue_impl)
    graph = _PipelineGraph(logger=logger)
    graph.add(source, tee, queue)
    graph.branch(tee, queue)
    tee_impl.release_fails = True
    pipeline.remove_failures.add("source")

    errors = graph.release()

    assert [str(error) for error in errors] == [
        "pad release failed",
        "Failed to remove source:source from GStreamer pipeline",
    ]
    assert pipeline.removed == [queue_impl]
    assert logger.warning.call_args_list == [
        call("gstreamer_graph_cleanup_failed", operation="release_request_pad", error="pad release failed"),
        call(
            "gstreamer_graph_cleanup_failed",
            operation="remove_element",
            error="Failed to remove source:source from GStreamer pipeline",
        ),
    ]

    tee_impl.release_fails = False
    pipeline.remove_failures.clear()
    assert graph.release() == ()
    assert pipeline.removed == [queue_impl, tee_impl, source_impl]
    assert graph.release() == ()


def test_failed_null_transition_does_not_destroy_live_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = FakePipeline()
    gst = _install_gst(monkeypatch, pipeline)
    pipeline.state_result = gst.StateChangeReturn.FAILURE
    source_impl = FakeElement("source")
    graph = _PipelineGraph(logger=Mock())
    graph.add(_element(source_impl))

    errors = graph.release()

    assert len(errors) == 1
    assert pipeline.removed == []
    assert not graph.cleanup_complete


def test_null_transition_can_unblock_inflight_graph_use(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = FakePipeline()
    _install_gst(monkeypatch, pipeline)
    graph = _PipelineGraph(logger=Mock())
    operation_entered = Event()

    def use_graph() -> None:
        def operation(owned: FakePipeline) -> None:
            operation_entered.set()
            assert owned.null_requested.wait(1)

        graph.use(operation)

    worker = Thread(target=use_graph, daemon=False)
    worker.start()
    assert operation_entered.wait(1)
    assert graph.release() == ()
    worker.join(1)
    assert not worker.is_alive()


def test_released_graph_rejects_all_graph_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_gst(monkeypatch, FakePipeline())
    graph = _PipelineGraph(logger=Mock())
    first = _element(FakeElement("first"))
    second = _element(FakeElement("second"))
    tee = _tee(FakeElement("tee"))
    graph.release()

    operations = (
        lambda: graph.use(lambda pipeline: pipeline),
        lambda: graph.add(first),
        lambda: graph.link(first, second),
        lambda: graph.branch(tee, first),
    )
    for operation in operations:
        with pytest.raises(GStreamerElementError, match="released"):
            operation()


def test_pipeline_creation_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_gst(monkeypatch, None)

    with pytest.raises(GStreamerElementError, match="create GStreamer pipeline"):
        _PipelineGraph(logger=Mock())


def test_tee_does_not_own_request_pads() -> None:
    assert "link" not in Tee.__dict__
    assert "release_request_pads" not in Tee.__dict__
