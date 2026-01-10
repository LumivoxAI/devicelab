from __future__ import annotations

from lumivox_devicelab._gstreamer.elements.app import CapturePacketError, CapturePacketErrorKind
from lumivox_devicelab._gstreamer.capture_recovery import _CaptureHealth, _RestartBudget


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _error(kind: CapturePacketErrorKind) -> CapturePacketError:
    return CapturePacketError(kind, kind.value)


def test_mapping_window_triggers_at_three_and_expires_events_exactly_at_ten_seconds() -> None:
    clock = FakeClock()
    health = _CaptureHealth(clock)
    mapping = _error(CapturePacketErrorKind.MAPPING)

    assert not health.observe(mapping)
    clock.now = 1.0
    assert not health.observe(mapping)
    clock.now = 9.999
    assert health.observe(mapping)

    health.clear()
    clock.now = 20.0
    assert not health.observe(mapping)
    clock.now = 29.0
    assert not health.observe(mapping)
    clock.now = 30.0
    assert not health.observe(mapping)


def test_malformed_and_missing_timestamp_share_one_severe_window() -> None:
    clock = FakeClock()
    health = _CaptureHealth(clock)

    assert not health.observe(_error(CapturePacketErrorKind.MALFORMED))
    clock.now = 5.0
    assert health.observe(_error(CapturePacketErrorKind.MISSING_TIMESTAMP))
    assert health.observe(_error(CapturePacketErrorKind.CAPS))


def test_restart_budget_consumes_fixed_delays_and_resets_only_after_sixty_stable_seconds() -> None:
    clock = FakeClock()
    budget = _RestartBudget(clock)

    delays = [budget.next_delay(), budget.next_delay(), budget.next_delay(), budget.next_delay()]
    assert delays == [0.25, 1.0, 4.0, None]
    budget.mark_running()
    clock.now = 59.999
    budget.begin_recovery()
    assert budget.next_delay() is None
    budget.mark_running()
    clock.now = 120.0
    budget.begin_recovery()
    assert budget.next_delay() == 0.25


def test_restart_budget_does_not_count_recovery_downtime_as_stable() -> None:
    clock = FakeClock()
    budget = _RestartBudget(clock)
    assert budget.next_delay() == 0.25
    clock.now = 59.9
    budget.begin_recovery()
    clock.now = 120.0
    assert budget.next_delay() == 1.0
