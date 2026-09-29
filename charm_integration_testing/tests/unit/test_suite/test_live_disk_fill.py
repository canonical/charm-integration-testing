# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import MagicMock

import pytest
from juju import JujuModelHandle
from test_suite.test_live_disk_fill import _avail_mb_from_df
from test_suite.test_live_disk_fill import test_live_disk_fill as run_disk_fill

MODEL = JujuModelHandle(controller="controller", model="model")


@pytest.mark.parametrize("failure", [None, "baseline", "df", "fill", "observe", "cleanup", "recovery", "validation"])
def test_disk_lifecycle(failure: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    juju, backend, chaos = MagicMock(), MagicMock(), MagicMock()
    waits = iter(["baseline", "recovery"])

    def event(name: str) -> None:
        events.append(name)
        if failure == name:
            raise RuntimeError(name)

    juju.multi_model_idle_for_period.side_effect = lambda **kwargs: event(next(waits))
    juju.validate_model.side_effect = lambda **kwargs: event("validation")
    chaos.fill_disk.side_effect = lambda **kwargs: event("fill")
    chaos.cleanup.side_effect = lambda **kwargs: event("cleanup")
    backend.exec_unit.return_value.return_code = 0
    backend.exec_unit.return_value.stdout = (
        "Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/test 200000 10000 100000 5% /"
    )
    if failure == "df":
        backend.exec_unit.side_effect = RuntimeError("df")
    monkeypatch.setattr("test_suite.test_live_disk_fill.sleep", lambda seconds: event("observe"))
    if failure:
        with pytest.raises(RuntimeError, match=failure):
            run_disk_fill(juju, backend, chaos, MODEL, "target", None)
    else:
        run_disk_fill(juju, backend, chaos, MODEL, "target", None)
        assert events == ["baseline", "fill", "observe", "cleanup", "recovery", "validation"]
        juju.validate_model.assert_called_once_with(model=MODEL, level="deep")
    if failure not in {"baseline", "df"}:
        chaos.cleanup.assert_called_once()
        assert chaos.cleanup.call_args.kwargs["path"] == chaos.fill_disk.call_args.kwargs["path"]
    else:
        chaos.fill_disk.assert_not_called()
        chaos.cleanup.assert_not_called()
    if failure in {"baseline", "df", "fill", "observe", "cleanup"}:
        assert "recovery" not in events
        juju.validate_model.assert_not_called()


@pytest.mark.parametrize("neighbor", [MODEL, JujuModelHandle(controller="other", model="neighbor")])
def test_neighbor_validation(neighbor: JujuModelHandle, monkeypatch: pytest.MonkeyPatch) -> None:
    juju, backend, chaos = MagicMock(), MagicMock(), MagicMock()
    backend.exec_unit.return_value.return_code = 0
    backend.exec_unit.return_value.stdout = "header\n/dev/test 200000 10000 100000 5% /"
    monkeypatch.setattr("test_suite.test_live_disk_fill.sleep", lambda seconds: None)
    run_disk_fill(juju, backend, chaos, MODEL, "target", neighbor)
    models = [MODEL] if neighbor == MODEL else [MODEL, neighbor]
    assert [call.kwargs for call in juju.validate_model.call_args_list] == [
        {"model": model, "level": "deep"} for model in models
    ]


@pytest.mark.parametrize("output", ["", "header", "header\ninvalid", "header\nx 1 2 invalid"])
def test_invalid_df(output: str) -> None:
    with pytest.raises(ValueError):
        _avail_mb_from_df(output)
