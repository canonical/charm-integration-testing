# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest
from chaos_client import (
    ChaosCleanupError,
    ChaosClient,
    ChaosMeshChaosClient,
    ChaosMeshNotInstalledError,
    MetaChaosClient,
)
from juju import JujuModelHandle
from kubernetes import client as k8s  # type: ignore[import-untyped]
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend

from .shared import FakeCustomObjectsApi

TEST_MODEL = JujuModelHandle(controller="test-controller", model="test-model")
UNIT = "postgresql/0"
SELECTOR = {"namespaces": ["test-model"], "labelSelectors": {"app.kubernetes.io/name": "postgresql"}}


class BackendStub(KubernetesBackend):
    def __init__(
        self,
        *,
        crds: tuple[str, ...] = ("stresschaos.chaos-mesh.org", "iochaos.chaos-mesh.org"),
        raise_on_delete: ApiException | None = None,
    ) -> None:
        self.core_v1_api = MagicMock()
        self.core_v1_api.list_namespaced_pod.return_value = k8s.V1PodList(
            items=[
                k8s.V1Pod(
                    metadata=k8s.V1ObjectMeta(name="target-pod", annotations={"unit.juju.is/id": UNIT}),
                    spec=k8s.V1PodSpec(
                        containers=[
                            k8s.V1Container(
                                name="workload", env=[k8s.V1EnvVar(name="JUJU_CONTAINER_NAME", value="workload")]
                            )
                        ]
                    ),
                ),
                k8s.V1Pod(metadata=k8s.V1ObjectMeta(name="other-pod", annotations={"unit.juju.is/id": "postgresql/1"})),
            ]
        )
        self._crds = set(crds)
        self.crd_errors: dict[str, ApiException] = {}
        self.crd_reads: list[str] = []
        self.custom_objects_api = FakeCustomObjectsApi(raise_on_delete=raise_on_delete, inject_stress=True)

    def crd_exists(self, name: str) -> bool:
        self.crd_reads.append(name)
        if name in self.crd_errors:
            raise self.crd_errors[name]
        return name in self._crds


class FailedCreateApi(FakeCustomObjectsApi):
    def __init__(self, *, resource_exists: bool, error: Exception) -> None:
        super().__init__()
        self.resource_exists = resource_exists
        self.error = error

    def create_namespaced_custom_object(
        self, *, group: str, version: str, namespace: str, plural: str, body: dict[str, object]
    ) -> dict[str, Any]:
        created = super().create_namespaced_custom_object(
            group=group, version=version, namespace=namespace, plural=plural, body=body
        )
        if not self.resource_exists:
            del self.objects[(plural, namespace, created["metadata"]["name"])]
        raise self.error


class TestCreationFailureCleanup:
    @pytest.mark.parametrize("replaced", [False, True], ids=["original", "replacement"])
    @pytest.mark.parametrize("resource_exists", [True, False], ids=["created", "not-created"])
    @pytest.mark.parametrize("operation", ["stress_cpu", "io_latency"])
    def test_timeout_retains_resource_for_teardown(self, resource_exists: bool, operation: str, replaced: bool) -> None:
        # GIVEN a POST that times out, with or without a resource on the server
        error = TimeoutError("Lost create response")
        api = FailedCreateApi(resource_exists=resource_exists, error=error)
        backend = BackendStub()
        backend.custom_objects_api = api
        mesh = ChaosMeshChaosClient(backend)
        client = MetaChaosClient([mesh])

        # WHEN execution fails
        with pytest.raises(TimeoutError) as exc_info:
            if operation == "stress_cpu":
                client.stress_cpu(TEST_MODEL, UNIT, workers=1, duration=timedelta(seconds=10))
            else:
                client.io_latency(TEST_MODEL, UNIT, "/data", timedelta(seconds=1), 50, timedelta(seconds=10))
        assert exc_info.value is error
        assert len(mesh._created) == 1
        resource = mesh._created[0]
        expected_path = "" if operation == "stress_cpu" else "/data"
        assert mesh._scopes[resource[2]] == (TEST_MODEL.uri, UNIT, expected_path)

        # THEN an existing object cannot be identified, even with the original owner annotation
        if resource_exists:
            if replaced:
                api.objects[resource]["metadata"]["uid"] = "replacement"
            for _ in range(2):
                with pytest.raises(ChaosCleanupError) as cleanup_error:
                    client.cleanup_all()
                assert "Creation UID was not recorded" in str(cleanup_error.value.errors[0])
                assert mesh._created == [resource]
                assert mesh._uids == {}
                assert api.delete_calls == []
                assert resource in api.objects
            # Once an external actor removes it, a 404 safely clears the pending action.
            api.objects.clear()
        client.cleanup_all()
        assert api.delete_calls == []
        assert mesh._created == []
        assert mesh._scopes == {}

    def test_conflict_does_not_delete_existing_resource(self) -> None:
        # GIVEN a POST rejected because its resource name already exists
        error = ApiException(status=409)
        api = FailedCreateApi(resource_exists=True, error=error)
        backend = BackendStub()
        backend.custom_objects_api = api
        mesh = ChaosMeshChaosClient(backend)
        client = MetaChaosClient([mesh])

        # WHEN execution fails and teardown runs
        with pytest.raises(ApiException) as exc_info:
            client.stress_cpu(TEST_MODEL, UNIT, workers=1, duration=timedelta(seconds=10))
        client.cleanup_all()

        # THEN the original error propagates and the conflicting resource remains
        assert exc_info.value is error
        assert api.delete_calls == []
        assert len(api.objects) == 1
        assert mesh._created == []
        assert mesh._scopes == {}


class TestConstruction:
    """Test suite for ChaosMeshChaosClient construction."""

    def test_raises_when_chaos_mesh_is_absent(self) -> None:
        # GIVEN a backend stub without the Chaos Mesh CRD
        # WHEN constructing a ChaosMeshChaosClient
        # THEN a ChaosMeshNotInstalledError is raised
        with pytest.raises(ChaosMeshNotInstalledError, match="stresschaos.chaos-mesh.org"):
            ChaosMeshChaosClient(BackendStub(crds=()))

    @pytest.mark.parametrize("status", [401, 403, 500])
    @pytest.mark.parametrize("stress_present", [False, True])
    def test_crd_api_error_propagates(self, status: int, stress_present: bool) -> None:
        # GIVEN an IOChaos lookup failure, regardless of StressChaos availability
        backend = BackendStub(crds=("stresschaos.chaos-mesh.org",) if stress_present else ())
        error = ApiException(status=status)
        backend.crd_errors["iochaos.chaos-mesh.org"] = error

        # WHEN constructing a client, THEN the error is not treated as an absent CRD
        with pytest.raises(ApiException) as exc_info:
            ChaosMeshChaosClient(backend)
        assert exc_info.value is error
        assert backend.crd_reads == ["stresschaos.chaos-mesh.org", "iochaos.chaos-mesh.org"]
        assert backend.custom_objects_api.create_calls == []

    def test_succeeds_when_chaos_mesh_is_present(self) -> None:
        # GIVEN a backend stub with the Chaos Mesh CRD present
        # WHEN constructing a ChaosMeshChaosClient
        client = ChaosMeshChaosClient(BackendStub())

        # THEN no CRs are tracked yet
        assert client._created == []


class TestExperimentAvailability:
    @dataclass(frozen=True)
    class Params:
        operation: str
        plural: str
        invoke: Callable[[ChaosClient], None]
        path: str = ""

    test_cases = [
        Params("cpu", "stresschaos", lambda client: client.stress_cpu(TEST_MODEL, UNIT, 1, timedelta(seconds=10))),
        Params(
            "memory",
            "stresschaos",
            lambda client: client.stress_memory(TEST_MODEL, UNIT, 1, 128, timedelta(seconds=10)),
        ),
        Params(
            "io",
            "iochaos",
            lambda client: client.io_latency(
                TEST_MODEL, UNIT, "/data", timedelta(seconds=1), 50, timedelta(seconds=10)
            ),
            "/data",
        ),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=lambda params: params.operation)
    @pytest.mark.parametrize(
        "crds",
        [
            ("stresschaos.chaos-mesh.org", "iochaos.chaos-mesh.org"),
            ("stresschaos.chaos-mesh.org",),
            ("iochaos.chaos-mesh.org",),
        ],
        ids=["both", "stress-only", "io-only"],
    )
    def test_only_supported_experiments_create_resources(self, params: Params, crds: tuple[str, ...]) -> None:
        # GIVEN a client with all or some experiment CRDs
        backend = BackendStub(crds=crds)
        client = ChaosMeshChaosClient(backend)
        api = backend.custom_objects_api

        # WHEN an experiment is requested
        if f"{params.plural}.chaos-mesh.org" in crds:
            params.invoke(client)
            uid = next(iter(api.objects.values()))["metadata"]["uid"]
            client.cleanup(TEST_MODEL, UNIT, params.path)

            # THEN supported resources are created and cleaned up
            assert [call["plural"] for call in api.create_calls] == [params.plural]
            assert [call["plural"] for call in api.delete_calls] == [params.plural]
            options = api.delete_options[0]
            assert options is not None
            assert options.preconditions.uid == uid
        else:
            with pytest.raises(NotImplementedError, match=params.plural):
                params.invoke(client)
            client.cleanup(TEST_MODEL, UNIT, params.path)

            # THEN unsupported operations leave no resources or cleanup work
            assert api.create_calls == []
            assert api.delete_calls == []
        assert client._created == []
        assert client._scopes == {}

    def test_missing_capability_falls_back_to_next_client(self) -> None:
        # GIVEN stress-only Mesh followed by a client capable of I/O latency
        first = BackendStub(crds=("stresschaos.chaos-mesh.org",))
        second = BackendStub(crds=("iochaos.chaos-mesh.org",))
        client = MetaChaosClient([ChaosMeshChaosClient(first), ChaosMeshChaosClient(second)])

        # WHEN requesting latency and cleaning up
        client.io_latency(TEST_MODEL, UNIT, "/data", timedelta(seconds=1), 50, timedelta(seconds=10))
        client.cleanup_all()

        # THEN only the supporting client creates and cleans up a resource
        assert first.custom_objects_api.create_calls == []
        assert first.custom_objects_api.delete_calls == []
        assert [call["plural"] for call in second.custom_objects_api.create_calls] == ["iochaos"]
        assert [call["plural"] for call in second.custom_objects_api.delete_calls] == ["iochaos"]


class TestStressCpu:
    """Test suite for stress_cpu method."""

    def test_creates_stress_chaos_with_cpu_stressors(self) -> None:
        # GIVEN a client wrapping a backend stub
        backend = BackendStub()
        client = ChaosMeshChaosClient(backend)

        # WHEN stressing CPU for a unit
        client.stress_cpu(TEST_MODEL, UNIT, workers=2, duration=timedelta(seconds=30))

        # THEN a StressChaos CR is created with the expected spec
        assert len(backend.custom_objects_api.create_calls) == 1
        call = backend.custom_objects_api.create_calls[0]
        assert (call["group"], call["version"], call["plural"], call["namespace"]) == (
            "chaos-mesh.org",
            "v1alpha1",
            "stresschaos",
            "test-model",
        )
        body = call["body"]
        assert body["apiVersion"] == "chaos-mesh.org/v1alpha1"
        assert body["kind"] == "StressChaos"
        assert body["metadata"]["namespace"] == "test-model"
        assert body["metadata"]["name"].startswith("chaos-cpu-stress-postgresql-")
        assert body["spec"]["selector"] == {"pods": {"test-model": ["target-pod"]}}
        assert body["spec"]["stressors"] == {"cpu": {"workers": 2}}
        assert body["spec"]["duration"] == "30s"
        assert client._created == [("stresschaos", "test-model", body["metadata"]["name"])]


class TestStressMemory:
    """Test suite for stress_memory method."""

    def test_creates_stress_chaos_with_memory_stressors(self) -> None:
        # GIVEN a client wrapping a backend stub
        backend = BackendStub()
        client = ChaosMeshChaosClient(backend)

        # WHEN stressing memory for a unit
        client.stress_memory(TEST_MODEL, UNIT, workers=1, size_mb=256, duration=timedelta(minutes=1))

        # THEN a StressChaos CR is created with the expected spec
        body = backend.custom_objects_api.create_calls[0]["body"]
        assert body["kind"] == "StressChaos"
        assert body["spec"]["stressors"] == {"memory": {"workers": 1, "size": "256MB"}}
        assert body["spec"]["duration"] == "60s"


class TestIoLatency:
    """Test suite for io_latency method."""

    def test_creates_io_chaos_latency(self) -> None:
        # GIVEN a client wrapping a backend stub
        backend = BackendStub()
        client = ChaosMeshChaosClient(backend)

        # WHEN injecting I/O latency for a unit
        client.io_latency(
            TEST_MODEL,
            UNIT,
            volume_path="/var/lib/postgresql",
            delay=timedelta(seconds=5),
            percent=100,
            duration=timedelta(seconds=30),
        )

        # THEN an IOChaos CR is created with the expected spec
        call = backend.custom_objects_api.create_calls[0]
        assert call["plural"] == "iochaos"
        body = call["body"]
        assert body["kind"] == "IOChaos"
        assert body["metadata"]["name"].startswith("chaos-io-latency-postgresql-")
        assert body["spec"]["action"] == "latency"
        assert body["spec"]["selector"] == SELECTOR
        assert body["spec"]["volumePath"] == "/var/lib/postgresql"
        assert body["spec"]["delay"] == "5000ms"
        assert body["spec"]["percent"] == 100
        assert body["spec"]["duration"] == "30s"


class TestCleanup:
    """Test suite for cleanup method."""

    def test_deletes_every_created_cr_in_reverse_order(self) -> None:
        # GIVEN a client that has created several CRs
        backend = BackendStub()
        client = ChaosMeshChaosClient(backend)
        client.stress_cpu(TEST_MODEL, UNIT, workers=1, duration=timedelta(seconds=10))
        client.io_latency(
            TEST_MODEL,
            UNIT,
            volume_path="/data",
            delay=timedelta(seconds=1),
            percent=50,
            duration=timedelta(seconds=10),
        )
        created = list(client._created)

        # WHEN cleaning each experiment scope
        client.cleanup(TEST_MODEL, UNIT, path="/data")
        assert len(client._created) == 1
        client.cleanup(TEST_MODEL, UNIT, path="")

        # THEN every CR is deleted in reverse creation order and tracking is cleared
        deleted = [(c["plural"], c["namespace"], c["name"]) for c in backend.custom_objects_api.delete_calls]
        assert deleted == list(reversed(created))
        assert client._created == []

    def test_cleanup_preserves_other_models_units_and_paths(self) -> None:
        # GIVEN latency experiments differing in one scope field
        backend = BackendStub()
        client = ChaosMeshChaosClient(backend)
        other = JujuModelHandle(controller="other-controller", model=TEST_MODEL.model)
        targets = [
            (TEST_MODEL, UNIT, "/data"),
            (other, UNIT, "/data"),
            (TEST_MODEL, "postgresql/1", "/data"),
            (TEST_MODEL, UNIT, "/other"),
        ]
        for model, unit, path in targets:
            client.io_latency(model, unit, path, timedelta(seconds=1), 50, timedelta(seconds=10))
        created = list(client._created)

        # WHEN cleaning the first scope
        client.cleanup(TEST_MODEL, UNIT, "/data")

        # THEN other resource scopes remain tracked
        assert client._created == created[1:]
        assert [call["name"] for call in backend.custom_objects_api.delete_calls] == [created[0][2]]

    def test_swallows_404_from_delete(self) -> None:
        # GIVEN a client with a created CR whose delete returns 404
        backend = BackendStub(raise_on_delete=ApiException(status=404))
        client = ChaosMeshChaosClient(backend)
        client.stress_cpu(TEST_MODEL, UNIT, workers=1, duration=timedelta(seconds=10))

        # WHEN cleaning up
        client.cleanup(TEST_MODEL, UNIT, path="")

        # THEN no exception is raised and tracking is cleared
        assert client._created == []

    def test_reraises_non_404_from_delete(self) -> None:
        # GIVEN a client with a created CR whose delete returns 500
        backend = BackendStub(raise_on_delete=ApiException(status=500))
        client = ChaosMeshChaosClient(backend)
        client.stress_cpu(TEST_MODEL, UNIT, workers=1, duration=timedelta(seconds=10))

        # WHEN cleaning up
        # THEN the API exception is re-raised and the CR that failed to delete stays tracked for a retry
        with pytest.raises(ApiException):
            client.cleanup(TEST_MODEL, UNIT, path="")
        assert len(client._created) == 1

        # WHEN deletion recovers, THEN the retained resource can be removed
        backend.custom_objects_api.raise_on_delete = None
        client.cleanup(TEST_MODEL, UNIT, path="")
        assert client._created == []


class TestUnsupportedMethods:
    """Test suite for unsupported chaos methods."""

    def test_fill_disk_raises_not_implemented(self) -> None:
        # GIVEN a client wrapping a backend stub
        client = ChaosMeshChaosClient(BackendStub())

        # WHEN calling fill_disk
        # THEN it is unsupported for this backend
        with pytest.raises(NotImplementedError):
            client.fill_disk(TEST_MODEL, UNIT, path="/tmp/fill", size_mb=1)

    def test_isolate_network_raises_not_implemented(self) -> None:
        # GIVEN a client wrapping a backend stub
        client = ChaosMeshChaosClient(BackendStub())

        # WHEN calling isolate_network
        # THEN it is unsupported for this backend
        with pytest.raises(NotImplementedError):
            client.isolate_network("test-model", UNIT)

    def test_remove_network_isolation_raises_not_implemented(self) -> None:
        # GIVEN a client wrapping a backend stub
        client = ChaosMeshChaosClient(BackendStub())

        # WHEN calling remove_network_isolation
        # THEN it is unsupported for this backend
        with pytest.raises(NotImplementedError):
            client.remove_network_isolation("test-model", UNIT)


class TestCleanupIdentity:
    @pytest.mark.parametrize("ambiguous_create", [False, True])
    @pytest.mark.parametrize("identity", ["owner", "annotations", "uid"])
    def test_unverified_identity_retains_cleanup(self, ambiguous_create: bool, identity: str) -> None:
        backend = BackendStub()
        if ambiguous_create:
            backend.custom_objects_api = FailedCreateApi(resource_exists=True, error=TimeoutError())
        mesh = ChaosMeshChaosClient(backend)
        if ambiguous_create:
            with pytest.raises(TimeoutError):
                mesh.stress_cpu(TEST_MODEL, UNIT, 1, timedelta(seconds=10))
        else:
            mesh.stress_cpu(TEST_MODEL, UNIT, 1, timedelta(seconds=10))
        api = backend.custom_objects_api
        resource = mesh._created[0]
        metadata = api.objects[resource]["metadata"]
        if identity == "owner":
            metadata["annotations"]["charm-integration-testing/owner"] = "another-client"
        else:
            del metadata[identity]

        with pytest.raises(RuntimeError, match="Cannot verify|Creation UID was not recorded"):
            mesh.cleanup(TEST_MODEL, UNIT, "")

        assert api.delete_calls == []
        assert resource in api.objects
        assert mesh._created == [resource]
        assert mesh._scopes[resource[2]] == (TEST_MODEL.uri, UNIT, "")

    def test_replacement_with_same_owner_is_not_deleted(self) -> None:
        backend = BackendStub()
        mesh = ChaosMeshChaosClient(backend)
        mesh.stress_cpu(TEST_MODEL, UNIT, 1, timedelta(seconds=10))
        api = backend.custom_objects_api
        resource = mesh._created[0]
        api.objects[resource]["metadata"]["uid"] = "replacement-uid"

        with pytest.raises(RuntimeError, match="Cannot verify UID"):
            mesh.cleanup(TEST_MODEL, UNIT, "")

        assert api.delete_calls == []
        assert resource in api.objects
        assert mesh._created == [resource]

    @pytest.mark.parametrize("status", [403, 500])
    def test_read_failure_can_be_retried(self, status: int) -> None:
        backend = BackendStub()
        mesh = ChaosMeshChaosClient(backend)
        mesh.stress_cpu(TEST_MODEL, UNIT, 1, timedelta(seconds=10))
        api = backend.custom_objects_api
        resource = mesh._created[0]
        uid = api.objects[resource]["metadata"]["uid"]
        api.raise_on_read = ApiException(status=status)

        with pytest.raises(ApiException):
            mesh.cleanup(TEST_MODEL, UNIT, "")
        assert api.delete_calls == []
        assert mesh._created == [resource]

        api.raise_on_read = None
        mesh.cleanup(TEST_MODEL, UNIT, "")
        options = api.delete_options[0]
        assert options is not None
        assert options.preconditions.uid == uid
        assert api.objects == {}
        assert mesh._created == []
        assert mesh._uids == {}

    def test_replacement_during_delete_is_protected_on_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = BackendStub()
        api = backend.custom_objects_api
        mesh = ChaosMeshChaosClient(backend)
        mesh.stress_cpu(TEST_MODEL, UNIT, 1, timedelta(seconds=10))
        resource = mesh._created[0]
        uid = api.objects[resource]["metadata"]["uid"]
        delete = api.delete_namespaced_custom_object

        def replace_then_delete(**kwargs: Any) -> None:
            api.objects[resource]["metadata"]["uid"] = "replacement-uid"
            delete(**kwargs)

        monkeypatch.setattr(api, "delete_namespaced_custom_object", replace_then_delete)
        with pytest.raises(ApiException) as exc_info:
            mesh.cleanup(TEST_MODEL, UNIT, "")
        assert exc_info.value.status == 409
        options = api.delete_options[0]
        assert options is not None
        assert options.preconditions.uid == uid
        with pytest.raises(RuntimeError, match="Cannot verify UID"):
            mesh.cleanup(TEST_MODEL, UNIT, "")
        assert len(api.delete_calls) == 1
        assert api.objects[resource]["metadata"]["uid"] == "replacement-uid"
        assert mesh._created == [resource]

    def test_missing_resource_clears_tracking(self) -> None:
        backend = BackendStub()
        mesh = ChaosMeshChaosClient(backend)
        mesh.stress_cpu(TEST_MODEL, UNIT, 1, timedelta(seconds=10))
        api = backend.custom_objects_api
        api.objects.clear()

        mesh.cleanup(TEST_MODEL, UNIT, "")

        assert api.delete_calls == []
        assert mesh._created == []
        assert mesh._scopes == {}
        assert mesh._uids == {}


@pytest.mark.parametrize("operation", ["stress_cpu", "stress_memory"])
@pytest.mark.parametrize("matches", [0, 1, 2])
def test_stress_targets_exactly_one_live_unit_pod(operation: str, matches: int) -> None:
    # GIVEN other units and stale Pods sharing the selected unit annotation
    backend = BackendStub()
    pods = backend.core_v1_api.list_namespaced_pod.return_value.items
    target = pods.pop(0)

    for phase in ("Succeeded", "Failed"):
        stale = deepcopy(target)
        stale.status = k8s.V1PodStatus(phase=phase)
        pods.append(stale)
    stale = deepcopy(target)
    stale.metadata.deletion_timestamp = datetime.now(timezone.utc)
    pods.append(stale)
    pods.extend(deepcopy(target) for _ in range(matches))
    mesh = ChaosMeshChaosClient(backend)

    def run() -> None:
        if operation == "stress_cpu":
            mesh.stress_cpu(TEST_MODEL, UNIT, 1, timedelta(seconds=10))
        else:
            mesh.stress_memory(TEST_MODEL, UNIT, 1, 128, timedelta(seconds=10))

    # WHEN selecting a unit, THEN only its single live Pod can be stressed
    if matches == 1:
        run()
        body = backend.custom_objects_api.create_calls[0]["body"]
        assert body["spec"]["selector"] == {"pods": {"test-model": ["target-pod"]}}
    else:
        with pytest.raises(RuntimeError, match="Expected one live Pod"):
            run()
        assert backend.custom_objects_api.create_calls == []


@pytest.mark.parametrize("operation", ["stress_cpu", "stress_memory"])
def test_stress_waits_for_injection_before_returning(operation: str) -> None:
    # GIVEN a controller that reports unselected/pending before it injects the fault
    backend = BackendStub()
    api = backend.custom_objects_api
    api.inject_stress = False
    now = 0.0
    sleeps: list[float] = []

    def pause(seconds: float) -> None:
        nonlocal now
        now += seconds
        sleeps.append(seconds)
        obj = next(iter(api.objects.values()))
        obj["status"] = {
            "conditions": [
                {"type": "Selected", "status": "True" if len(sleeps) > 1 else "False"},
                {"type": "AllInjected", "status": "True"},
                {"type": "AllRecovered", "status": "False"},
            ],
            "experiment": {"desiredPhase": "Run"},
        }

    mesh = ChaosMeshChaosClient(backend, clock=lambda: now, pause=pause)
    if operation == "stress_cpu":
        mesh.stress_cpu(TEST_MODEL, UNIT, 4, timedelta(minutes=12))
    else:
        mesh.stress_memory(TEST_MODEL, UNIT, 1, 128, timedelta(minutes=12))
    # THEN resource creation alone, or AllInjected without Selected, cannot start the hold period.
    assert sleeps == [1.0, 1.0]
    assert now == 2
    mesh.cleanup(TEST_MODEL, UNIT, "")
    assert not api.objects


@pytest.mark.parametrize(
    "state", ["pending", "recovered", "stopped", "paused", "replacement", "wrong-owner", "api-error"]
)
def test_injection_failure_retains_cleanup_tracking(state: str) -> None:
    # GIVEN a created resource whose injection cannot be confirmed
    backend = BackendStub()
    api = backend.custom_objects_api
    api.inject_stress = False
    now = 0.0

    def pause(seconds: float) -> None:
        nonlocal now
        now += seconds
        obj = next(iter(api.objects.values()))
        if state == "stopped":
            obj["status"] = {"experiment": {"desiredPhase": "Stop"}}
        elif state == "paused":
            obj["status"] = {"conditions": [{"type": "Paused", "status": "True"}]}
        elif state == "recovered":
            obj["status"] = {
                "experiment": {"desiredPhase": "Run"},
                "conditions": [{"type": key, "status": "True"} for key in ("Selected", "AllInjected", "AllRecovered")],
            }
        elif state == "replacement":
            obj["metadata"]["uid"] = "replacement"
        elif state == "wrong-owner":
            obj["metadata"]["annotations"] = {}
        elif state == "api-error":
            api.raise_on_read = ApiException(status=403)

    mesh = ChaosMeshChaosClient(backend, startup_timeout=timedelta(seconds=3), clock=lambda: now, pause=pause)
    expected = (
        TimeoutError if state in {"pending", "recovered"} else (ApiException if state == "api-error" else RuntimeError)
    )
    with pytest.raises(expected):
        mesh.stress_cpu(TEST_MODEL, UNIT, 4, timedelta(minutes=12))
    assert now <= 3
    assert len(mesh._created) == 1
    assert not api.delete_calls
    api.raise_on_read = None
    # THEN ordinary failures remain cleanable; replaced or unowned resources are protected.
    if state in {"replacement", "wrong-owner"}:
        with pytest.raises(RuntimeError):
            mesh.cleanup(TEST_MODEL, UNIT, "")
        assert not api.delete_calls
    else:
        mesh.cleanup(TEST_MODEL, UNIT, "")
        assert not api.objects


def test_memory_targets_workload_container_only() -> None:
    backend = BackendStub()
    target = backend.core_v1_api.list_namespaced_pod.return_value.items[0]
    target.spec.containers.append(k8s.V1Container(name="charm"))
    chaos = ChaosMeshChaosClient(backend)
    chaos.stress_memory(TEST_MODEL, UNIT, 1, 2048, timedelta(minutes=10))
    body = backend.custom_objects_api.create_calls[-1]["body"]
    assert body["spec"]["containerNames"] == ["workload"]
    assert body["spec"]["selector"] == {"pods": {TEST_MODEL.model: ["target-pod"]}}


def test_memory_rejects_ambiguous_workload_containers() -> None:
    backend = BackendStub()
    target = backend.core_v1_api.list_namespaced_pod.return_value.items[0]
    target.spec.containers.append(
        k8s.V1Container(name="second", env=[k8s.V1EnvVar(name="JUJU_CONTAINER_NAME", value="second")])
    )
    with pytest.raises(RuntimeError, match="one workload container"):
        ChaosMeshChaosClient(backend).stress_memory(TEST_MODEL, UNIT, 1, 2048, timedelta(minutes=10))
    assert not backend.custom_objects_api.create_calls


class TestStressObservation:
    @pytest.mark.parametrize("allow_completed", [False, True])
    @pytest.mark.parametrize(
        "state",
        [
            "running",
            "completed",
            "stopped",
            "paused",
            "pause-requested",
            "deleting",
            "lost-injection",
            "record-not-injected",
            "apply-failed",
            "recover-failed",
            "unknown",
            "missing-status",
            "null-status",
            "wrong-owner",
            "replacement",
            "missing-uid",
            "missing-resource",
            "api-error",
        ],
    )
    def test_status_changes_after_injection(self, state: str, allow_completed: bool) -> None:
        # GIVEN an experiment whose initial injection succeeded
        backend = BackendStub()
        mesh = ChaosMeshChaosClient(backend)
        meta = MetaChaosClient([mesh])
        meta.stress_memory(TEST_MODEL, UNIT, 1, 2048, timedelta(minutes=10))
        api = backend.custom_objects_api
        resource = mesh._created[0]
        obj = api.objects[resource]
        conditions = obj["status"]["conditions"]
        conditions.append({"type": "AllRecovered", "status": "False"})
        record: dict[str, Any] = {"phase": "Injected", "events": []}
        obj["status"]["experiment"]["containerRecords"] = [record]
        if state in {"completed", "stopped", "recover-failed"}:
            obj["status"]["experiment"]["desiredPhase"] = "Stop"
            if state != "stopped":
                conditions[-1]["status"] = "True"
                record["phase"] = "Not Injected"
        if state in {"apply-failed", "recover-failed"}:
            record["events"] = [
                {
                    "type": "Failed",
                    "operation": "Apply" if state == "apply-failed" else "Recover",
                    "message": "daemon unavailable",
                }
            ]
        elif state == "paused":
            conditions.append({"type": "Paused", "status": "True"})
        elif state == "pause-requested":
            obj["metadata"]["annotations"]["experiment.chaos-mesh.org/pause"] = "true"
        elif state == "deleting":
            obj["metadata"]["deletionTimestamp"] = "2026-09-30T00:00:00Z"
        elif state == "lost-injection":
            conditions[1]["status"] = "False"
        elif state == "record-not-injected":
            record["phase"] = "Not Injected"
        elif state == "unknown":
            conditions[0]["status"] = "Unknown"
        elif state == "missing-status":
            del obj["status"]
        elif state == "null-status":
            obj["status"] = None
        elif state == "wrong-owner":
            obj["metadata"]["annotations"] = {}
        elif state == "replacement":
            obj["metadata"]["uid"] = "replacement"
        elif state == "missing-uid":
            mesh._uids.clear()
        elif state == "missing-resource":
            api.objects.clear()
        elif state == "api-error":
            api.raise_on_read = ApiException(status=503)

        # WHEN observing, THEN only active or explicitly allowed completed runs pass
        if state == "running" or (state == "completed" and allow_completed):
            meta.check_stress(TEST_MODEL, UNIT, allow_completed=allow_completed)
        else:
            expected = ApiException if state in {"missing-resource", "api-error"} else RuntimeError
            with pytest.raises(expected):
                meta.check_stress(TEST_MODEL, UNIT, allow_completed=allow_completed)
        assert mesh._created == [resource]
        assert not api.delete_calls

        # Failures do not lose the cleanup registration.
        api.raise_on_read = None
        if state not in {"wrong-owner", "replacement", "missing-uid"}:
            meta.cleanup_all()
            assert not mesh._created

    @pytest.mark.parametrize("scope", ["unit", "controller", "io"])
    def test_ignores_unrelated_experiments(self, scope: str) -> None:
        backend = BackendStub()
        mesh = ChaosMeshChaosClient(backend)
        if scope == "io":
            mesh.io_latency(TEST_MODEL, UNIT, "/data", timedelta(seconds=1), 50, timedelta(seconds=30))
        else:
            mesh.stress_cpu(TEST_MODEL, UNIT, 1, timedelta(seconds=30))
        backend.custom_objects_api.raise_on_read = ApiException(status=403)
        model = JujuModelHandle(controller="other", model=TEST_MODEL.model) if scope == "controller" else TEST_MODEL
        mesh.check_stress(model, "postgresql/1" if scope == "unit" else UNIT)
