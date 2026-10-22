# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from time import monotonic
from unittest.mock import MagicMock

import pytest
from chaos_client import IoLatencyInjection
from juju import JujuModelHandle
from test_suite import test_live_disk_io_latency as module
from test_suite.test_live_disk_io_latency import (
    DELAY,
    LATENCY_DURATION,
    PERCENT,
    VOLUME_PATH,
)
from test_suite.test_live_disk_io_latency import (
    test_live_disk_io_latency as run_disk_io_latency,
)

MODEL = JujuModelHandle(controller="controller", model="model")
# Deliberately different from LATENCY_DURATION, to prove the test observes for however long
# io_latency() says the experiment was actually configured to run (e.g. a per-charm override),
# rather than the caller-supplied duration.
RESOLVED_DURATION = timedelta(minutes=20)


@pytest.mark.parametrize("failure", [None, "baseline", "io_latency", "observe", "cleanup", "recovery", "validation"])
def test_disk_io_latency_lifecycle(failure: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    juju, chaos = MagicMock(), MagicMock()
    waits = iter(["baseline", "recovery"])
    hold = MagicMock()
    # The experiment was created at t=0; 5s elapsed (e.g. awaiting confirmed injection) by
    # the time observation starts, so observation must account for it.
    injection = IoLatencyInjection(duration=RESOLVED_DURATION, injected_at=0.0)

    def event(name: str) -> None:
        events.append(name)
        if failure == name:
            raise RuntimeError(name)

    def fake_io_latency(**kwargs: object) -> IoLatencyInjection:
        event("io_latency")
        return injection

    def fake_observe(chaos: object, model: object, unit: object, volume_path: object, seconds: float) -> None:
        hold(seconds)
        event("observe")

    juju.multi_model_idle_for_period.side_effect = lambda **kwargs: event(next(waits))
    juju.validate_model.side_effect = lambda **kwargs: event("validation")
    chaos.io_latency.side_effect = fake_io_latency
    chaos.cleanup_all.side_effect = lambda: event("cleanup")
    monkeypatch.setattr(module, "monotonic", lambda: 5.0)
    monkeypatch.setattr(module, "observe_io_latency", fake_observe)

    if failure:
        with pytest.raises(RuntimeError, match=failure):
            run_disk_io_latency(juju, chaos, MODEL, "target", None)
    else:
        run_disk_io_latency(juju, chaos, MODEL, "target", None)
        assert events == ["baseline", "io_latency", "observe", "cleanup", "recovery", "validation"]
        juju.validate_model.assert_called_once_with(model=MODEL, level="deep")
        chaos.io_latency.assert_called_once_with(
            model=MODEL,
            unit="target/0",
            volume_path=VOLUME_PATH,
            delay=DELAY,
            percent=PERCENT,
            duration=LATENCY_DURATION,
        )
        # Observation must use the resolved duration minus the 5s spent confirming injection,
        # not the full (possibly stale) resolved value.
        hold.assert_called_once_with(RESOLVED_DURATION.total_seconds() - 5.0)

    # The finally block runs cleanup_all whenever the try block was entered, i.e. for every
    # failure except "baseline" (which fails before the try block is reached).
    if failure != "baseline":
        chaos.cleanup_all.assert_called_once()
    else:
        chaos.cleanup_all.assert_not_called()

    if failure in {"baseline", "io_latency", "observe", "cleanup"}:
        assert "recovery" not in events
        juju.validate_model.assert_not_called()


def test_skips_before_baseline_wait_when_unsupported() -> None:
    juju, chaos = MagicMock(), MagicMock()
    chaos.supports.return_value = False

    with pytest.raises(pytest.skip.Exception, match="io_latency"):
        run_disk_io_latency(juju, chaos, MODEL, "target", None)

    chaos.supports.assert_called_once_with("io_latency")
    juju.multi_model_idle_for_period.assert_not_called()
    chaos.io_latency.assert_not_called()


def test_observation_duration_clamped_when_confirmation_outlasts_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A short per-charm duration may fully elapse while io_latency() awaits confirmation."""
    juju, chaos = MagicMock(), MagicMock()
    hold = MagicMock()
    # Confirmation alone takes longer than the resolved duration.
    monkeypatch.setattr(module, "monotonic", lambda: RESOLVED_DURATION.total_seconds() + 5.0)
    monkeypatch.setattr(module, "observe_io_latency", lambda chaos, model, unit, volume_path, seconds: hold(seconds))
    chaos.io_latency.return_value = IoLatencyInjection(duration=RESOLVED_DURATION, injected_at=0.0)

    run_disk_io_latency(juju, chaos, MODEL, "target", None)

    # Never pass a negative remaining duration; observe_io_latency must still check for a
    # normally completed status instead of being skipped outright.
    hold.assert_called_once_with(0.0)


def test_reports_both_errors_when_observation_and_cleanup_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cleanup failure must not hide the original fault that failed the live test."""
    juju, chaos = MagicMock(), MagicMock()
    chaos.io_latency.return_value = IoLatencyInjection(duration=RESOLVED_DURATION, injected_at=monotonic())
    monkeypatch.setattr(module, "observe_io_latency", MagicMock(side_effect=RuntimeError("fault not injected")))
    chaos.cleanup_all.side_effect = RuntimeError("cleanup unreachable")

    with pytest.raises(RuntimeError, match="fault not injected") as excinfo:
        run_disk_io_latency(juju, chaos, MODEL, "target", None)

    # The original observation error must be preserved (not replaced), chained from the
    # cleanup failure so neither error is lost.
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "cleanup unreachable" in str(excinfo.value.__cause__)


def test_keyboard_interrupt_during_observation_still_runs_cleanup_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator interruption must not be swallowed, and cleanup must still run."""
    juju, chaos = MagicMock(), MagicMock()
    chaos.io_latency.return_value = IoLatencyInjection(duration=RESOLVED_DURATION, injected_at=monotonic())
    monkeypatch.setattr(module, "observe_io_latency", MagicMock(side_effect=KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        run_disk_io_latency(juju, chaos, MODEL, "target", None)

    chaos.cleanup_all.assert_called_once()


@pytest.mark.parametrize("neighbor", [MODEL, JujuModelHandle(controller="other", model="neighbor")])
def test_neighbor_validation(neighbor: JujuModelHandle, monkeypatch: pytest.MonkeyPatch) -> None:
    juju, chaos = MagicMock(), MagicMock()
    chaos.io_latency.return_value = IoLatencyInjection(duration=RESOLVED_DURATION, injected_at=monotonic())
    monkeypatch.setattr(module, "observe_io_latency", lambda *args, **kwargs: None)
    run_disk_io_latency(juju, chaos, MODEL, "target", neighbor)
    models = [MODEL] if neighbor == MODEL else [MODEL, neighbor]
    assert [call.kwargs for call in juju.validate_model.call_args_list] == [
        {"model": model, "level": "deep"} for model in models
    ]


@pytest.mark.parametrize("fail", [False, True])
def test_observation_polls_and_stops_on_error(monkeypatch: pytest.MonkeyPatch, fail: bool) -> None:
    now = 0.0
    chaos = MagicMock()

    def pause(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(module, "monotonic", lambda: now)
    monkeypatch.setattr(module, "sleep", pause)
    if fail:
        chaos.check_io_latency.side_effect = [None, RuntimeError("injection failed")]
        with pytest.raises(RuntimeError, match="injection failed"):
            module.observe_io_latency(chaos, MODEL, "target/0", VOLUME_PATH, 25)
        assert now == module.CHECK_INTERVAL.total_seconds()
    else:
        module.observe_io_latency(chaos, MODEL, "target/0", VOLUME_PATH, 25)
        assert now == 25
        assert chaos.check_io_latency.call_count == 4
        chaos.check_io_latency.assert_called_with(MODEL, "target/0", VOLUME_PATH, allow_completed=True)


def test_observation_allows_completion_without_extra_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    chaos = MagicMock()
    pause = MagicMock()
    monkeypatch.setattr(module, "sleep", pause)
    module.observe_io_latency(chaos, MODEL, "target/0", VOLUME_PATH, 0)
    chaos.check_io_latency.assert_called_once_with(MODEL, "target/0", VOLUME_PATH, allow_completed=True)
    pause.assert_not_called()


def test_observation_tolerates_completion_detected_during_slow_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow status read can straddle the deadline; normal completion then must not fail."""
    now = [0.0]

    def fake_monotonic() -> float:
        return now[0]

    def slow_check(*args: object, **kwargs: object) -> None:
        # The read is slow enough that the deadline passes while it is in flight.
        now[0] = 25.0
        if not kwargs["allow_completed"]:
            raise RuntimeError("fault not injected")

    chaos = MagicMock()
    chaos.check_io_latency.side_effect = slow_check
    monkeypatch.setattr(module, "monotonic", fake_monotonic)
    monkeypatch.setattr(module, "sleep", MagicMock())

    module.observe_io_latency(chaos, MODEL, "target/0", VOLUME_PATH, 25)

    assert chaos.check_io_latency.call_count == 2
    chaos.check_io_latency.assert_called_with(MODEL, "target/0", VOLUME_PATH, allow_completed=True)


def test_observation_still_fails_when_slow_read_completion_is_not_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine failure during a slow read must still propagate, not just a timing straddle."""
    now = [0.0]

    def fake_monotonic() -> float:
        return now[0]

    def slow_check(*args: object, **kwargs: object) -> None:
        now[0] = 25.0
        raise RuntimeError("fault not injected")

    chaos = MagicMock()
    chaos.check_io_latency.side_effect = slow_check
    monkeypatch.setattr(module, "monotonic", fake_monotonic)
    monkeypatch.setattr(module, "sleep", MagicMock())

    with pytest.raises(RuntimeError, match="fault not injected"):
        module.observe_io_latency(chaos, MODEL, "target/0", VOLUME_PATH, 25)

    assert chaos.check_io_latency.call_count == 2


def test_observation_skips_sleep_when_successful_read_crosses_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow but successful read must not sleep using a stale, already-expired remaining time."""
    now = [0.0]

    def fake_monotonic() -> float:
        return now[0]

    def slow_check(*args: object, **kwargs: object) -> None:
        # The read itself takes longer than the originally remaining time.
        now[0] = 30.0

    chaos = MagicMock()
    chaos.check_io_latency.side_effect = slow_check
    pause = MagicMock()
    monkeypatch.setattr(module, "monotonic", fake_monotonic)
    monkeypatch.setattr(module, "sleep", pause)

    module.observe_io_latency(chaos, MODEL, "target/0", VOLUME_PATH, 25)

    chaos.check_io_latency.assert_called_once_with(MODEL, "target/0", VOLUME_PATH, allow_completed=False)
    pause.assert_not_called()
