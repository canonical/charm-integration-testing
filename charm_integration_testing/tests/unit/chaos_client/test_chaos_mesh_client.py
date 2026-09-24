# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from chaos_client import ChaosMeshChaosClient, ChaosMeshNotInstalledError, MetaChaosClient
from juju import JujuModelHandle
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
        self._crds = set(crds)
        self.custom_objects_api = FakeCustomObjectsApi(raise_on_delete=raise_on_delete)

    def crd_exists(self, name: str) -> bool:
        return name in self._crds


class FailedCreateApi(FakeCustomObjectsApi):
    def __init__(self, *, resource_exists: bool, error: Exception) -> None:
        super().__init__()
        self.resource_exists = resource_exists
        self.error = error
        self.resources: set[tuple[str, str, str]] = set()

    def create_namespaced_custom_object(
        self, *, group: str, version: str, namespace: str, plural: str, body: dict[str, object]
    ) -> None:
        super().create_namespaced_custom_object(
            group=group, version=version, namespace=namespace, plural=plural, body=body
        )
        metadata = body["metadata"]
        assert isinstance(metadata, dict)
        name = metadata["name"]
        assert isinstance(name, str)
        if self.resource_exists:
            self.resources.add((plural, namespace, name))
        raise self.error

    def delete_namespaced_custom_object(
        self, *, group: str, version: str, namespace: str, plural: str, name: str
    ) -> None:
        super().delete_namespaced_custom_object(
            group=group, version=version, namespace=namespace, plural=plural, name=name
        )
        resource = (plural, namespace, name)
        if resource not in self.resources:
            raise ApiException(status=404)
        self.resources.remove(resource)


class TestCreationFailureCleanup:
    @pytest.mark.parametrize("resource_exists", [True, False], ids=["created", "not-created"])
    @pytest.mark.parametrize("operation", ["stress_cpu", "io_latency"])
    def test_timeout_retains_resource_for_teardown(self, resource_exists: bool, operation: str) -> None:
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

        # THEN teardown deletes the recorded resource or tolerates its absence
        client.cleanup_all()
        assert [(call["plural"], call["namespace"], call["name"]) for call in api.delete_calls] == [resource]
        assert api.resources == set()
        assert mesh._created == []
        assert mesh._scopes == {}
        client.cleanup_all()
        assert len(api.delete_calls) == 1

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
        assert len(api.resources) == 1
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

    def test_raises_when_iochaos_crd_is_missing(self) -> None:
        # GIVEN a backend stub with only the stresschaos CRD present
        # WHEN constructing a ChaosMeshChaosClient
        # THEN a ChaosMeshNotInstalledError is raised naming the missing CRD
        with pytest.raises(ChaosMeshNotInstalledError, match="iochaos"):
            ChaosMeshChaosClient(BackendStub(crds=("stresschaos.chaos-mesh.org",)))

    def test_succeeds_when_chaos_mesh_is_present(self) -> None:
        # GIVEN a backend stub with the Chaos Mesh CRD present
        # WHEN constructing a ChaosMeshChaosClient
        client = ChaosMeshChaosClient(BackendStub())

        # THEN no CRs are tracked yet
        assert client._created == []


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
        assert body["spec"]["selector"] == SELECTOR
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
