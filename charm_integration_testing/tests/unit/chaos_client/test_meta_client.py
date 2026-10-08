# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

import pytest
from chaos_client import (
    ChaosCleanupError,
    ChaosClient,
    ChaosNotSupportedError,
    ChaosResourceConstraintsError,
    CharmResourceConstraints,
    MetaChaosClient,
    ResourceConstraintsClient,
)
from juju import CharmChannel as JujuCharmChannel
from juju import JujuApplicationInfo, JujuModelHandle

from ..extensions.shared import NullJujuBackend

TEST_MODEL = JujuModelHandle(controller="test-controller", model="test-model")
UNIT = "postgresql/0"
DURATION = timedelta(seconds=30)


class BackendStub(NullJujuBackend):
    def __init__(
        self,
        applications: dict[str, JujuApplicationInfo] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.applications = applications or {}
        self.error = error

    def list_applications(self, model: JujuModelHandle) -> dict[str, JujuApplicationInfo]:
        if self.error is not None:
            raise self.error
        return self.applications


class ConstraintsClientStub(ResourceConstraintsClient):
    def __init__(self, constraint: CharmResourceConstraints | None = None) -> None:
        super().__init__(constraints_dir=None)
        self.constraint = constraint or CharmResourceConstraints()
        self.calls: list[tuple[str, str, str]] = []

    def get_charm_resource_constraints(
        self, charm: str, channel: object, ubuntu_version: str
    ) -> CharmResourceConstraints:
        self.calls.append((charm, str(channel), ubuntu_version))
        return self.constraint


def backend_with_application() -> BackendStub:
    return BackendStub(
        {
            "postgresql": JujuApplicationInfo(
                charm="postgresql-k8s",
                revision=1,
                channel=JujuCharmChannel.parse("14/stable"),
                base="22.04",
            )
        }
    )


# Resolvable metadata by default, so tests unrelated to constraint resolution itself
# don't have to care about it: a lookup failure now raises instead of silently
# applying no constraints (see ChaosResourceConstraintsError).
DEFAULT_BACKEND = backend_with_application()
DEFAULT_CONSTRAINTS_CLIENT = ResourceConstraintsClient()


class ClientStub(ChaosClient):
    def __init__(self, supported: set[str]) -> None:
        self.supported = supported
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.errors: dict[str, BaseException] = {}

    def supports(self, operation: str) -> bool:
        return operation in self.supported

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
    client = MetaChaosClient([unsupported, supporting, unused], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)

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
    client = MetaChaosClient([] if empty else [tool], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)

    # WHEN requesting the experiment, THEN a specific unsupported error is raised
    with pytest.raises(ChaosNotSupportedError, match=params.operation):
        params.invoke(client)

    # THEN unsupported calls leave no cleanup work
    client.cleanup(TEST_MODEL, UNIT, "")
    client.remove_network_isolation(TEST_MODEL.model, UNIT)
    assert all(operation == params.operation for operation, _ in tool.calls)


@pytest.mark.parametrize("params", EXPERIMENTS, ids=lambda params: params.operation)
def test_reports_unsupported_experiment_without_resolving_constraints(params: Params) -> None:
    # GIVEN no configured client and a backend that cannot resolve chaos resource constraints
    client = MetaChaosClient([], BackendStub(error=RuntimeError("list failed")), DEFAULT_CONSTRAINTS_CLIENT)

    # WHEN requesting the experiment
    # THEN the experiment is reported as unsupported rather than surfacing a resource
    # constraints failure: with no client to run it, constraints are never resolved.
    with pytest.raises(ChaosNotSupportedError, match=params.operation):
        params.invoke(client)


@pytest.mark.parametrize("params", EXPERIMENTS, ids=lambda params: params.operation)
def test_reports_unsupported_experiment_with_non_supporting_tool_present(params: Params) -> None:
    # GIVEN a configured tool that never supports this operation (e.g. a disk-fill-only
    # client, always present in the real fixture) and a backend that cannot resolve
    # chaos resource constraints
    other_operation = next(p.operation for p in EXPERIMENTS if p.operation != params.operation)
    tool = ClientStub({other_operation})
    client = MetaChaosClient([tool], BackendStub(error=RuntimeError("list failed")), DEFAULT_CONSTRAINTS_CLIENT)

    # WHEN requesting the experiment
    # THEN the experiment is reported as unsupported rather than surfacing a resource
    # constraints failure: a non-empty tool list must not trigger constraint resolution
    # unless a configured tool actually supports this operation.
    with pytest.raises(ChaosNotSupportedError, match=params.operation):
        params.invoke(client)


def test_selects_a_client_for_each_experiment() -> None:
    # GIVEN a stress-only client followed by one supporting both experiments
    stress_only = ClientStub({"stress_cpu"})
    mesh = ClientStub({"stress_cpu", "io_latency"})
    client = MetaChaosClient([stress_only, mesh], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)

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
    client = MetaChaosClient([failing, fallback], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)

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
    client = MetaChaosClient([native], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)
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
    client = MetaChaosClient([stress, disk], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)
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
    client = MetaChaosClient([native], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)
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
    client = MetaChaosClient([unsupported, network], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)
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
    client = MetaChaosClient([owner, fallback], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)
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
    client = MetaChaosClient([network, fallback], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)

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
    client = MetaChaosClient([disk, network], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)
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
    client = MetaChaosClient([tool], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)
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


def test_resolve_constraints_raises_when_application_is_missing() -> None:
    # GIVEN no application matching the unit name
    client = MetaChaosClient([ClientStub(set())], BackendStub(), DEFAULT_CONSTRAINTS_CLIENT)

    # WHEN resolving constraints
    # THEN the lookup failure is not swallowed into an unconstrained default
    with pytest.raises(ChaosResourceConstraintsError, match="Incomplete application metadata"):
        client._resolve_constraints(TEST_MODEL, UNIT)


@pytest.mark.parametrize(
    ("channel", "base"),
    [(None, "22.04"), (JujuCharmChannel.parse("14/stable"), None)],
    ids=["missing-channel", "missing-base"],
)
def test_resolve_constraints_raises_without_complete_metadata(
    channel: JujuCharmChannel | None, base: str | None
) -> None:
    # GIVEN the application exists but lacks channel or base metadata
    backend = BackendStub(
        {
            "postgresql": JujuApplicationInfo(
                charm="postgresql-k8s",
                revision=1,
                channel=channel,
                base=base,
            )
        }
    )
    client = MetaChaosClient([ClientStub(set())], backend, DEFAULT_CONSTRAINTS_CLIENT)

    # WHEN resolving constraints
    # THEN the lookup failure is not swallowed into an unconstrained default
    with pytest.raises(ChaosResourceConstraintsError, match="Incomplete application metadata"):
        client._resolve_constraints(TEST_MODEL, UNIT)


def test_resolve_constraints_raises_when_backend_listing_fails() -> None:
    # GIVEN a backend that cannot list applications
    backend_error = RuntimeError("list failed")
    client = MetaChaosClient(
        [ClientStub(set())],
        BackendStub(error=backend_error),
        DEFAULT_CONSTRAINTS_CLIENT,
    )

    # WHEN resolving constraints
    # THEN the failure propagates instead of being swallowed into an unconstrained default
    with pytest.raises(ChaosResourceConstraintsError) as excinfo:
        client._resolve_constraints(TEST_MODEL, UNIT)
    assert excinfo.value.__cause__ is backend_error


def test_resolve_constraints_uses_matching_application_metadata() -> None:
    # GIVEN application metadata and a constraints client
    constraints = CharmResourceConstraints(cpu_exhaustion_workers=5)
    constraints_client = ConstraintsClientStub(constraints)
    backend = BackendStub(
        {
            "postgresql": JujuApplicationInfo(
                charm="postgresql-k8s",
                revision=1,
                channel=JujuCharmChannel.parse("14/stable"),
                base="22.04",
            )
        }
    )
    client = MetaChaosClient([ClientStub(set())], backend, constraints_client)

    # WHEN resolving constraints
    resolved = client._resolve_constraints(TEST_MODEL, UNIT)

    # THEN the charm, channel and base are passed through to the lookup client
    assert resolved == constraints
    assert constraints_client.calls == [("postgresql-k8s", "14/stable", "22.04")]


def test_stress_cpu_exhaustion_uses_constraint_overrides() -> None:
    # GIVEN an exhaustion constraint overriding all CPU values
    tool = ClientStub({"stress_cpu"})
    client = MetaChaosClient(
        [tool],
        backend_with_application(),
        ConstraintsClientStub(CharmResourceConstraints(cpu_exhaustion_workers=7, cpu_exhaustion_duration_seconds=45)),
    )

    # WHEN stressing CPU
    client.stress_cpu(TEST_MODEL, UNIT, 2, DURATION)

    # THEN the merged values are dispatched
    assert tool.calls == [("stress_cpu", (TEST_MODEL, UNIT, 7, timedelta(seconds=45)))]


def test_stress_cpu_moderate_pressure_overrides_only_configured_fields() -> None:
    # GIVEN a moderate-pressure constraint overriding only worker count
    tool = ClientStub({"stress_cpu"})
    client = MetaChaosClient(
        [tool],
        backend_with_application(),
        ConstraintsClientStub(CharmResourceConstraints(cpu_moderate_pressure_workers=4)),
    )

    # WHEN stressing CPU under moderate pressure
    client.stress_cpu(TEST_MODEL, UNIT, 2, DURATION, scenario="moderate_pressure")

    # THEN unset fields fall back to the caller values
    assert tool.calls == [("stress_cpu", (TEST_MODEL, UNIT, 4, DURATION))]


def test_stress_cpu_without_matching_constraints_preserves_caller_values() -> None:
    # GIVEN no configured constraints
    tool = ClientStub({"stress_cpu"})
    client = MetaChaosClient([tool], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)

    # WHEN stressing CPU
    client.stress_cpu(TEST_MODEL, UNIT, 2, DURATION)

    # THEN the original values are preserved
    assert tool.calls == [("stress_cpu", (TEST_MODEL, UNIT, 2, DURATION))]


def test_stress_cpu_rejects_unsupported_scenario() -> None:
    # GIVEN a client with no matching constraints
    tool = ClientStub({"stress_cpu"})
    client = MetaChaosClient([tool], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)

    # WHEN stressing CPU with an unrecognized scenario
    with pytest.raises(ValueError, match="Unsupported CPU stress scenario"):
        client.stress_cpu(TEST_MODEL, UNIT, 2, DURATION, scenario="exhaustionn")  # type: ignore[arg-type]

    # THEN no call reaches the underlying tool
    assert tool.calls == []


@pytest.mark.parametrize("margin", [timedelta(0), timedelta(minutes=2)])
def test_stress_memory_exhaustion_uses_constraint_overrides(margin: timedelta) -> None:
    # GIVEN an exhaustion constraint overriding all memory values
    tool = ClientStub({"stress_memory"})
    client = MetaChaosClient(
        [tool],
        backend_with_application(),
        ConstraintsClientStub(
            CharmResourceConstraints(
                memory_exhaustion_workers=3,
                memory_exhaustion_size_mb=2048,
                memory_exhaustion_duration_seconds=90,
            )
        ),
    )

    # WHEN stressing memory
    client.stress_memory(TEST_MODEL, UNIT, 1, 512, DURATION, duration_margin=margin)

    # THEN the merged values are dispatched
    assert tool.calls == [("stress_memory", (TEST_MODEL, UNIT, 3, 2048, timedelta(seconds=90) + margin))]


def test_stress_memory_rejects_negative_duration_margin() -> None:
    tool = ClientStub({"stress_memory"})
    client = MetaChaosClient([tool], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)

    with pytest.raises(ValueError, match="duration margin must not be negative"):
        client.stress_memory(TEST_MODEL, UNIT, 1, 512, DURATION, duration_margin=timedelta(seconds=-1))

    assert tool.calls == []


def test_stress_memory_moderate_pressure_overrides_only_configured_fields() -> None:
    # GIVEN a moderate-pressure constraint overriding only size
    tool = ClientStub({"stress_memory"})
    client = MetaChaosClient(
        [tool],
        backend_with_application(),
        ConstraintsClientStub(CharmResourceConstraints(memory_moderate_pressure_size_mb=256)),
    )

    # WHEN stressing memory under moderate pressure
    client.stress_memory(TEST_MODEL, UNIT, 2, 512, DURATION, scenario="moderate_pressure")

    # THEN the unset fields fall back to caller values
    assert tool.calls == [("stress_memory", (TEST_MODEL, UNIT, 2, 256, DURATION))]


def test_stress_memory_without_matching_constraints_preserves_caller_values() -> None:
    # GIVEN no configured constraints
    tool = ClientStub({"stress_memory"})
    client = MetaChaosClient([tool], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)

    # WHEN stressing memory
    client.stress_memory(TEST_MODEL, UNIT, 2, 128, DURATION)

    # THEN the original values are preserved
    assert tool.calls == [("stress_memory", (TEST_MODEL, UNIT, 2, 128, DURATION))]


def test_stress_memory_rejects_unsupported_scenario() -> None:
    # GIVEN a client with no matching constraints
    tool = ClientStub({"stress_memory"})
    client = MetaChaosClient([tool], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)

    # WHEN stressing memory with an unrecognized scenario
    with pytest.raises(ValueError, match="Unsupported memory stress scenario"):
        client.stress_memory(TEST_MODEL, UNIT, 2, 128, DURATION, scenario="exhaustionn")  # type: ignore[arg-type]

    # THEN no call reaches the underlying tool
    assert tool.calls == []


def test_stress_memory_does_not_run_when_constraints_lookup_fails() -> None:
    # GIVEN a backend that cannot resolve the unit's application metadata
    tool = ClientStub({"stress_memory"})
    client = MetaChaosClient(
        [tool],
        BackendStub(error=RuntimeError("list failed")),
        DEFAULT_CONSTRAINTS_CLIENT,
    )

    # WHEN stressing memory
    # THEN the experiment is never dispatched: a failed lookup must not silently
    # run with the caller's (possibly charm-inappropriate) values
    with pytest.raises(ChaosResourceConstraintsError):
        client.stress_memory(TEST_MODEL, UNIT, 2, 128, DURATION)
    assert tool.calls == []


def test_fill_disk_uses_constraint_override() -> None:
    # GIVEN a disk-fill size override
    tool = ClientStub({"fill_disk"})
    client = MetaChaosClient(
        [tool],
        backend_with_application(),
        ConstraintsClientStub(CharmResourceConstraints(disk_fill_size_mb=1024)),
    )

    # WHEN filling disk
    client.fill_disk(TEST_MODEL, UNIT, "/data/fill", 128)

    # THEN the configured size is used
    assert tool.calls == [("fill_disk", (TEST_MODEL, UNIT, "/data/fill", 1024))]


def test_io_latency_uses_constraint_overrides() -> None:
    # GIVEN latency constraints overriding all I/O parameters
    tool = ClientStub({"io_latency"})
    client = MetaChaosClient(
        [tool],
        backend_with_application(),
        ConstraintsClientStub(
            CharmResourceConstraints(
                disk_io_latency_delay_ms=250,
                disk_io_latency_percent=70,
                disk_io_latency_duration_seconds=75,
            )
        ),
    )

    # WHEN injecting I/O latency
    client.io_latency(TEST_MODEL, UNIT, "/data", timedelta(milliseconds=50), 80, DURATION)

    # THEN the merged values are dispatched
    assert tool.calls == [
        ("io_latency", (TEST_MODEL, UNIT, "/data", timedelta(milliseconds=250), 70, timedelta(seconds=75)))
    ]


def test_io_latency_without_matching_constraints_preserves_caller_values() -> None:
    # GIVEN no configured constraints
    tool = ClientStub({"io_latency"})
    client = MetaChaosClient([tool], DEFAULT_BACKEND, DEFAULT_CONSTRAINTS_CLIENT)

    # WHEN injecting I/O latency
    client.io_latency(TEST_MODEL, UNIT, "/data", timedelta(milliseconds=50), 80, DURATION)

    # THEN the original values are preserved
    assert tool.calls == [("io_latency", (TEST_MODEL, UNIT, "/data", timedelta(milliseconds=50), 80, DURATION))]
