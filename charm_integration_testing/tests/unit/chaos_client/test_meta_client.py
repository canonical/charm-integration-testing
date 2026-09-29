# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

import pytest
from chaos_client import ChaosCleanupError, ChaosClient, ChaosNotSupportedError, MetaChaosClient
from juju import JujuModelHandle

TEST_MODEL = JujuModelHandle(controller="test-controller", model="test-model")
UNIT = "postgresql/0"
DURATION = timedelta(seconds=30)


class ClientStub(ChaosClient):
    def __init__(self, supported: set[str]) -> None:
        self.supported = supported
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.errors: dict[str, BaseException] = {}

    def _call(self, operation: str, *args: object) -> None:
        self.calls.append((operation, args))
        if operation in self.errors:
            raise self.errors[operation]
        if operation not in self.supported:
            raise NotImplementedError

    def fill_disk(self, model: JujuModelHandle, unit: str, path: str, size_mb: int) -> None:
        self._call("fill_disk", model, unit, path, size_mb)

    def stress_cpu(self, model: JujuModelHandle, unit: str, workers: int, duration: timedelta) -> None:
        self._call("stress_cpu", model, unit, workers, duration)

    def stress_memory(self, model: JujuModelHandle, unit: str, workers: int, size_mb: int, duration: timedelta) -> None:
        self._call("stress_memory", model, unit, workers, size_mb, duration)

    def io_latency(
        self,
        model: JujuModelHandle,
        unit: str,
        volume_path: str,
        delay: timedelta,
        percent: int,
        duration: timedelta,
    ) -> None:
        self._call("io_latency", model, unit, volume_path, delay, percent, duration)

    def cleanup(self, model: JujuModelHandle, unit: str, path: str) -> None:
        self._call("cleanup", model, unit, path)

    def isolate_network(self, model: str, unit: str) -> None:
        self._call("isolate_network", model, unit)

    def remove_network_isolation(self, model: str, unit: str) -> None:
        self._call("remove_network_isolation", model, unit)


@dataclass(frozen=True)
class Params:
    operation: str
    invoke: Callable[[ChaosClient], None]
    args: tuple[object, ...]


EXPERIMENTS = [
    Params(
        operation="fill_disk",
        invoke=lambda client: client.fill_disk(TEST_MODEL, UNIT, "/tmp/fill", 128),
        args=(TEST_MODEL, UNIT, "/tmp/fill", 128),
    ),
    Params(
        operation="stress_cpu",
        invoke=lambda client: client.stress_cpu(TEST_MODEL, UNIT, 2, DURATION),
        args=(TEST_MODEL, UNIT, 2, DURATION),
    ),
    Params(
        operation="stress_memory",
        invoke=lambda client: client.stress_memory(TEST_MODEL, UNIT, 2, 128, DURATION),
        args=(TEST_MODEL, UNIT, 2, 128, DURATION),
    ),
    Params(
        operation="io_latency",
        invoke=lambda client: client.io_latency(TEST_MODEL, UNIT, "/data", timedelta(milliseconds=50), 80, DURATION),
        args=(TEST_MODEL, UNIT, "/data", timedelta(milliseconds=50), 80, DURATION),
    ),
    Params(
        operation="isolate_network",
        invoke=lambda client: client.isolate_network(TEST_MODEL.model, UNIT),
        args=(TEST_MODEL.model, UNIT),
    ),
]


@pytest.mark.parametrize("params", EXPERIMENTS, ids=lambda params: params.operation)
def test_falls_back_only_until_a_supporting_client_is_found(params: Params) -> None:
    # GIVEN one unsupported client followed by two supporting clients
    unsupported = ClientStub(set())
    supporting = ClientStub({params.operation})
    unused = ClientStub({params.operation})
    client = MetaChaosClient([unsupported, supporting, unused])

    # WHEN requesting an experiment
    params.invoke(client)

    # THEN arguments are preserved and dispatch stops at the first supporting client
    assert unsupported.calls == [(params.operation, params.args)]
    assert supporting.calls == [(params.operation, params.args)]
    assert unused.calls == []


@pytest.mark.parametrize("params", EXPERIMENTS, ids=lambda params: params.operation)
@pytest.mark.parametrize("empty", [False, True], ids=["unsupported", "no-clients"])
def test_reports_unsupported_experiment(params: Params, empty: bool) -> None:
    # GIVEN no client capable of the requested experiment
    tool = ClientStub(set())
    client = MetaChaosClient([] if empty else [tool])

    # WHEN requesting the experiment, THEN a specific unsupported error is raised
    with pytest.raises(ChaosNotSupportedError, match=params.operation):
        params.invoke(client)

    # THEN unsupported calls leave no cleanup work
    client.cleanup(TEST_MODEL, UNIT, "")
    client.remove_network_isolation(TEST_MODEL.model, UNIT)
    assert all(operation == params.operation for operation, _ in tool.calls)


def test_selects_a_client_for_each_experiment() -> None:
    # GIVEN a stress-only client followed by one supporting both experiments
    stress_only = ClientStub({"stress_cpu"})
    mesh = ClientStub({"stress_cpu", "io_latency"})
    client = MetaChaosClient([stress_only, mesh])

    # WHEN requesting stress followed by I/O latency
    client.stress_cpu(TEST_MODEL, UNIT, 2, DURATION)
    client.io_latency(TEST_MODEL, UNIT, "/data", timedelta(milliseconds=50), 80, DURATION)

    # THEN each experiment uses the first supporting client
    assert [operation for operation, _ in stress_only.calls] == ["stress_cpu", "io_latency"]
    assert [operation for operation, _ in mesh.calls] == ["io_latency"]


@pytest.mark.parametrize(
    "error", [RuntimeError("execution failed"), KeyboardInterrupt()], ids=["failure", "interrupted"]
)
def test_execution_failure_propagates_and_retains_cleanup(error: BaseException) -> None:
    # GIVEN an execution that can fail after creating resources
    failing = ClientStub({"stress_cpu", "cleanup"})
    failing.errors["stress_cpu"] = error
    fallback = ClientStub({"stress_cpu", "cleanup"})
    client = MetaChaosClient([failing, fallback])

    # WHEN execution fails, THEN it is not retried with another tool
    with pytest.raises(type(error)) as exc_info:
        client.stress_cpu(TEST_MODEL, UNIT, 2, DURATION)
    assert exc_info.value is error
    assert fallback.calls == []

    # WHEN cleaning up, THEN the failed client's cleanup is still attempted
    client.cleanup(TEST_MODEL, UNIT, "")
    assert failing.calls[-1] == ("cleanup", (TEST_MODEL, UNIT, ""))
    assert fallback.calls == []


def test_disk_cleanup_preserves_each_created_path() -> None:
    # GIVEN two disk experiments on the same unit
    native = ClientStub({"fill_disk", "cleanup"})
    client = MetaChaosClient([native])
    client.fill_disk(TEST_MODEL, UNIT, "/tmp/first", 128)
    client.fill_disk(TEST_MODEL, UNIT, "/tmp/second", 256)

    # WHEN cleaning an unknown path and then only the first file
    client.cleanup(TEST_MODEL, UNIT, "/unused")
    assert len(native.calls) == 2
    client.cleanup(TEST_MODEL, UNIT, "/tmp/first")

    # THEN the second file remains pending until teardown
    assert native.calls[2:] == [("cleanup", (TEST_MODEL, UNIT, "/tmp/first"))]
    client.cleanup_all()
    assert native.calls[3:] == [("cleanup", (TEST_MODEL, UNIT, "/tmp/second"))]


def test_cleanup_attempts_all_owners_and_retains_failures_for_retry() -> None:
    # GIVEN two experiments whose respective clients both fail to clean up
    stress = ClientStub({"stress_cpu", "cleanup"})
    disk = ClientStub({"fill_disk", "cleanup"})
    stress_error = RuntimeError("stress cleanup failed")
    disk_error = RuntimeError("disk cleanup failed")
    stress.errors["cleanup"] = stress_error
    disk.errors["cleanup"] = disk_error
    client = MetaChaosClient([stress, disk])
    client.stress_cpu(TEST_MODEL, UNIT, 2, DURATION)
    client.fill_disk(TEST_MODEL, UNIT, "/tmp/fill", 128)

    # WHEN cleaning up, THEN both failures are preserved
    with pytest.raises(ChaosCleanupError) as exc_info:
        client.cleanup_all()
    assert exc_info.value.errors == (disk_error, stress_error)
    assert exc_info.value.__cause__ is disk_error

    # WHEN one owner recovers, THEN only failed cleanup is retried
    disk.errors.clear()
    with pytest.raises(ChaosCleanupError) as exc_info:
        client.cleanup_all()
    assert exc_info.value.errors == (stress_error,)
    disk_calls = list(disk.calls)
    stress.errors.clear()
    client.cleanup_all()
    client.cleanup_all()
    assert disk.calls == disk_calls
    assert [operation for operation, _ in stress.calls].count("cleanup") == 3


def test_cleanup_dispatch_keeps_model_and_unit_scopes_separate() -> None:
    # GIVEN experiments on distinct controllers and units
    other_model = JujuModelHandle(controller="other-controller", model=TEST_MODEL.model)
    native = ClientStub({"fill_disk", "cleanup"})
    client = MetaChaosClient([native])
    client.fill_disk(TEST_MODEL, UNIT, "/tmp/first", 128)
    client.fill_disk(other_model, UNIT, "/tmp/second", 128)
    client.fill_disk(TEST_MODEL, "postgresql/1", "/tmp/third", 128)

    # WHEN cleaning one target
    client.cleanup(TEST_MODEL, UNIT, "/tmp/first")

    # THEN only that target's cleanup is dispatched
    assert native.calls[3:] == [("cleanup", (TEST_MODEL, UNIT, "/tmp/first"))]


def test_network_removal_uses_the_execution_owner() -> None:
    # GIVEN a network experiment supported only by the second client
    unsupported = ClientStub(set())
    network = ClientStub({"isolate_network", "remove_network_isolation"})
    client = MetaChaosClient([unsupported, network])
    client.isolate_network(TEST_MODEL.model, UNIT)

    # WHEN removing isolation twice
    client.remove_network_isolation(TEST_MODEL.model, UNIT)
    client.remove_network_isolation(TEST_MODEL.model, UNIT)

    # THEN only the owner removes it, once
    assert network.calls == [
        ("isolate_network", (TEST_MODEL.model, UNIT)),
        ("remove_network_isolation", (TEST_MODEL.model, UNIT)),
    ]
    assert unsupported.calls == [("isolate_network", (TEST_MODEL.model, UNIT))]


def test_unsupported_cleanup_is_a_failure_not_a_fallback() -> None:
    # GIVEN an execution owner that does not implement cleanup
    owner = ClientStub({"stress_cpu"})
    fallback = ClientStub({"stress_cpu", "cleanup"})
    client = MetaChaosClient([owner, fallback])
    client.stress_cpu(TEST_MODEL, UNIT, 2, DURATION)

    # WHEN cleaning up, THEN missing cleanup is surfaced rather than delegated
    with pytest.raises(ChaosCleanupError) as exc_info:
        client.cleanup(TEST_MODEL, UNIT, "")
    assert len(exc_info.value.errors) == 1
    assert isinstance(exc_info.value.errors[0], NotImplementedError)
    assert fallback.calls == []


def test_failed_network_execution_retains_removal_action() -> None:
    # GIVEN a failed isolation request which might have created a policy
    network = ClientStub({"isolate_network", "remove_network_isolation"})
    error = RuntimeError("network request failed")
    network.errors["isolate_network"] = error
    fallback = ClientStub({"isolate_network"})
    client = MetaChaosClient([network, fallback])

    # WHEN isolation fails, THEN the failure propagates without fallback
    with pytest.raises(RuntimeError) as exc_info:
        client.isolate_network(TEST_MODEL.model, UNIT)
    assert exc_info.value is error
    assert fallback.calls == []

    # WHEN removing isolation, THEN the original client handles removal
    client.remove_network_isolation(TEST_MODEL.model, UNIT)
    assert network.calls[-1] == ("remove_network_isolation", (TEST_MODEL.model, UNIT))


def test_cleanup_all_continues_after_network_removal_fails() -> None:
    # GIVEN disk fill and network isolation on different clients
    disk = ClientStub({"fill_disk", "cleanup"})
    network = ClientStub({"isolate_network", "remove_network_isolation"})
    error = RuntimeError("network cleanup failed")
    network.errors["remove_network_isolation"] = error
    client = MetaChaosClient([disk, network])
    client.fill_disk(TEST_MODEL, UNIT, "/tmp/fill", 128)
    client.isolate_network(TEST_MODEL.model, UNIT)

    # WHEN network removal fails, THEN disk cleanup still runs
    with pytest.raises(ChaosCleanupError) as exc_info:
        client.cleanup_all()
    assert exc_info.value.errors == (error,)
    assert disk.calls[-1] == ("cleanup", (TEST_MODEL, UNIT, "/tmp/fill"))
    disk_calls = list(disk.calls)

    # WHEN retrying, THEN only network removal remains
    network.errors.clear()
    client.cleanup_all()
    assert disk.calls == disk_calls
    assert network.calls[-1] == ("remove_network_isolation", (TEST_MODEL.model, UNIT))


def test_path_cleanup_preserves_stress_and_other_latency_paths() -> None:
    # GIVEN stress and two latency experiments on the same unit
    tool = ClientStub({"stress_cpu", "io_latency", "cleanup"})
    client = MetaChaosClient([tool])
    client.stress_cpu(TEST_MODEL, UNIT, 1, DURATION)
    for path in ("/data", "/other"):
        client.io_latency(TEST_MODEL, UNIT, path, timedelta(seconds=1), 50, DURATION)

    # WHEN cleaning one latency path
    client.cleanup(TEST_MODEL, UNIT, "/data")

    # THEN stress and the other path remain until teardown
    assert tool.calls[3:] == [("cleanup", (TEST_MODEL, UNIT, "/data"))]
    client.cleanup_all()
    assert tool.calls[4:] == [
        ("cleanup", (TEST_MODEL, UNIT, "/other")),
        ("cleanup", (TEST_MODEL, UNIT, "")),
    ]
