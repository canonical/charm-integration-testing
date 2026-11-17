# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from chaos_client import ChaosCleanupError
from juju import JujuModelHandle
from test_suite.test_live_disk_fill import _avail_mb_from_df, _storage_paths
from test_suite.test_live_disk_fill import test_live_disk_fill as run_disk_fill

MODEL = JujuModelHandle(controller="controller", model="model")


def storage_backend(paths: tuple[str, ...] = ("/data",)) -> MagicMock:
    backend = MagicMock()

    def execute(model: JujuModelHandle, unit: str, command: str) -> SimpleNamespace:
        if command == "storage-list --format=json":
            output = json.dumps([f"data/{i}" for i in range(len(paths))])
        elif command.startswith("storage-get"):
            index = int(command.split()[2].split("/")[1])
            output = json.dumps({"kind": "filesystem", "location": paths[index]})
        elif command.startswith("mountpoint"):
            output = str(paths.index(command.split()[-1]) + 1)
        else:
            output = "Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/test 200000 10000 100000 5% /data"
        return SimpleNamespace(return_code=0, stdout=output, stderr="")

    backend.exec_unit.side_effect = execute
    return backend


@pytest.mark.parametrize("failure", [None, "baseline", "df", "fill", "observe", "cleanup", "recovery", "validation"])
def test_disk_lifecycle(failure: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    juju, backend, chaos = MagicMock(), storage_backend(), MagicMock()
    waits = iter(["baseline", "recovery"])

    def event(name: str) -> None:
        events.append(name)
        if failure == name:
            raise RuntimeError(name)

    juju.multi_model_idle_for_period.side_effect = lambda **kwargs: event(next(waits))
    juju.validate_model.side_effect = lambda **kwargs: event("validation")
    chaos.fill_disk.side_effect = lambda **kwargs: event("fill")
    chaos.cleanup.side_effect = lambda **kwargs: event("cleanup")
    if failure == "df":
        execute: Callable[[JujuModelHandle, str, str], SimpleNamespace] = backend.exec_unit.side_effect

        def fail_df(model: JujuModelHandle, unit: str, command: str) -> SimpleNamespace:
            if command.startswith("df "):
                raise RuntimeError("df")
            return execute(model, unit, command)

        backend.exec_unit.side_effect = fail_df
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


def test_file_remaining_after_cleanup_fails_before_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    # GIVEN successful allocation and cleanup calls, but the fill file remains
    juju, backend, chaos = MagicMock(), storage_backend(), MagicMock()

    storage_execute: Callable[[JujuModelHandle, str, str], SimpleNamespace] = backend.exec_unit.side_effect

    def execute(model: JujuModelHandle, unit: str, command: str) -> SimpleNamespace:
        if command.startswith("test ! -e "):
            chaos.cleanup.assert_called_once()
            return SimpleNamespace(return_code=1)
        return storage_execute(model, unit, command)

    backend.exec_unit.side_effect = execute
    monkeypatch.setattr("test_suite.test_live_disk_fill.sleep", lambda seconds: None)

    # WHEN removal verification fails, THEN recovery and validation cannot succeed
    with pytest.raises(ChaosCleanupError) as error:
        run_disk_fill(juju, backend, chaos, MODEL, "target", None)

    assert "was not removed from target/0 during cleanup" in str(error.value.errors[0])
    path = chaos.fill_disk.call_args.kwargs["path"]
    chaos.cleanup.assert_called_once_with(model=MODEL, unit="target/0", path=path)
    backend.exec_unit.assert_called_with(MODEL, "target/0", f"test ! -e {path}")
    assert juju.multi_model_idle_for_period.call_count == 1  # Baseline only
    juju.validate_model.assert_not_called()


@pytest.mark.parametrize("neighbor", [MODEL, JujuModelHandle(controller="other", model="neighbor")])
def test_neighbor_validation(neighbor: JujuModelHandle, monkeypatch: pytest.MonkeyPatch) -> None:
    juju, backend, chaos = MagicMock(), storage_backend(), MagicMock()
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


def test_no_storage_skips() -> None:
    juju, chaos = MagicMock(), MagicMock()
    with pytest.raises(pytest.skip.Exception, match="No persistent storage"):
        run_disk_fill(juju, storage_backend(()), chaos, MODEL, "target", None)
    chaos.fill_disk.assert_not_called()
    juju.validate_model.assert_not_called()


@pytest.mark.parametrize("failure", [None, "fill", "cleanup"])
def test_multiple_volumes(failure: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    juju, chaos = MagicMock(), MagicMock()
    backend = storage_backend(("/data", "/logs"))
    events = []

    def fill(model: JujuModelHandle, unit: str, path: str, size_mb: int) -> None:
        events.append(("fill", path))
        if failure == "fill" and path.startswith("/logs/"):
            raise RuntimeError("allocation failed")

    def cleanup(model: JujuModelHandle, unit: str, path: str) -> None:
        events.append(("cleanup", path))
        if failure == "cleanup" and path.startswith("/logs/"):
            raise RuntimeError("removal failed")

    chaos.fill_disk.side_effect = fill
    chaos.cleanup.side_effect = cleanup
    monkeypatch.setattr("test_suite.test_live_disk_fill.sleep", lambda seconds: events.append(("hold", "")))
    if failure:
        with pytest.raises(RuntimeError):
            run_disk_fill(juju, backend, chaos, MODEL, "target", None)
        juju.validate_model.assert_not_called()
    else:
        run_disk_fill(juju, backend, chaos, MODEL, "target", None)
        juju.validate_model.assert_called_once_with(model=MODEL, level="deep")
    assert [name for name, _ in events] == (
        ["fill", "fill", "cleanup", "cleanup"] if failure == "fill" else ["fill", "fill", "hold", "cleanup", "cleanup"]
    )
    assert events[0][1].startswith("/data/chaos-disk-fill-")
    assert events[1][1].startswith("/logs/chaos-disk-fill-")
    assert {path for name, path in events if name == "fill"} == {path for name, path in events if name == "cleanup"}


@pytest.mark.parametrize("command", ["storage-list", "storage-get", "mountpoint"])
def test_storage_lookup_failure(command: str) -> None:
    backend = storage_backend()
    execute: Callable[[JujuModelHandle, str, str], SimpleNamespace] = backend.exec_unit.side_effect

    def fail(model: JujuModelHandle, unit: str, task: str) -> SimpleNamespace:
        if task.startswith(command):
            return SimpleNamespace(return_code=1, stdout="", stderr="lookup failed")
        return execute(model, unit, task)

    backend.exec_unit.side_effect = fail
    chaos = MagicMock()
    with pytest.raises(AssertionError):
        run_disk_fill(MagicMock(), backend, chaos, MODEL, "target", None)
    chaos.fill_disk.assert_not_called()


def test_storage_aliases_are_filled_once() -> None:
    backend = storage_backend(("/data", "/alias"))
    execute: Callable[[JujuModelHandle, str, str], SimpleNamespace] = backend.exec_unit.side_effect

    def same_filesystem(model: JujuModelHandle, unit: str, command: str) -> SimpleNamespace:
        if command.startswith("mountpoint"):
            return SimpleNamespace(return_code=0, stdout="42", stderr="")
        return execute(model, unit, command)

    backend.exec_unit.side_effect = same_filesystem
    assert _storage_paths(backend, MODEL, "target/0") == ["/data"]


@pytest.mark.parametrize(
    "storage",
    [
        {"kind": "block", "location": "/dev/sdb"},
        {"kind": "filesystem", "location": "relative"},
        {"kind": "filesystem", "location": "/"},
    ],
)
def test_invalid_storage(storage: dict[str, str]) -> None:
    backend = storage_backend()
    execute: Callable[[JujuModelHandle, str, str], SimpleNamespace] = backend.exec_unit.side_effect

    def get_storage(model: JujuModelHandle, unit: str, command: str) -> SimpleNamespace:
        if command.startswith("storage-get"):
            return SimpleNamespace(return_code=0, stdout=json.dumps(storage), stderr="")
        return execute(model, unit, command)

    backend.exec_unit.side_effect = get_storage
    with pytest.raises(ValueError):
        _storage_paths(backend, MODEL, "target/0")
