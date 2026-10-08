# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import MagicMock

import pytest
from juju import JujuModelHandle
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


@pytest.mark.parametrize("failure", [None, "baseline", "io_latency", "observe", "cleanup", "recovery", "validation"])
def test_disk_io_latency_lifecycle(failure: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    juju, chaos = MagicMock(), MagicMock()
    waits = iter(["baseline", "recovery"])

    def event(name: str) -> None:
        events.append(name)
        if failure == name:
            raise RuntimeError(name)

    juju.multi_model_idle_for_period.side_effect = lambda **kwargs: event(next(waits))
    juju.validate_model.side_effect = lambda **kwargs: event("validation")
    chaos.io_latency.side_effect = lambda **kwargs: event("io_latency")
    chaos.cleanup_all.side_effect = lambda: event("cleanup")
    monkeypatch.setattr("test_suite.test_live_disk_io_latency.sleep", lambda seconds: event("observe"))

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
    monkeypatch.setattr("test_suite.test_live_disk_io_latency.sleep", lambda seconds: None)
    run_disk_io_latency(juju, chaos, MODEL, "target", neighbor)
    models = [MODEL] if neighbor == MODEL else [MODEL, neighbor]
    assert [call.kwargs for call in juju.validate_model.call_args_list] == [
        {"model": model, "level": "deep"} for model in models
    ]
