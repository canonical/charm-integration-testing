# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
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

    def event(name: str) -> None:
        events.append(name)
        if failure == name:
            raise RuntimeError(name)

    def fake_io_latency(**kwargs: object) -> timedelta:
        event("io_latency")
        return RESOLVED_DURATION

    def fake_observe(chaos: object, model: object, unit: object, volume_path: object, seconds: float) -> None:
        hold(seconds)
        event("observe")

    juju.multi_model_idle_for_period.side_effect = lambda **kwargs: event(next(waits))
    juju.validate_model.side_effect = lambda **kwargs: event("validation")
    chaos.io_latency.side_effect = fake_io_latency
    chaos.cleanup_all.side_effect = lambda: event("cleanup")
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
        # Observation must use the resolved duration, not the caller-supplied one.
        hold.assert_called_once_with(RESOLVED_DURATION.total_seconds())

    # The finally block runs cleanup_all whenever the try block was entered, i.e. for every
    # failure except "baseline" (which fails before the try block is reached).
    if failure != "baseline":
        chaos.cleanup_all.assert_called_once()
    else:
        chaos.cleanup_all.assert_not_called()

    if failure in {"baseline", "io_latency", "observe", "cleanup"}:
        assert "recovery" not in events
        juju.validate_model.assert_not_called()


@pytest.mark.parametrize("neighbor", [MODEL, JujuModelHandle(controller="other", model="neighbor")])
def test_neighbor_validation(neighbor: JujuModelHandle, monkeypatch: pytest.MonkeyPatch) -> None:
    juju, chaos = MagicMock(), MagicMock()
    chaos.io_latency.return_value = RESOLVED_DURATION
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
