# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from chaos_client import KubernetesChaosClient
from juju import JujuModelHandle
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend

TEST_MODEL = JujuModelHandle(controller="test-controller", model="test-model")


class FakeNetworkingV1Api:
    def __init__(self) -> None:
        self.create_calls: list[tuple[str, object]] = []
        self.delete_calls: list[tuple[str, str]] = []
        self.raise_on_delete: Exception | None = None

    def create_namespaced_network_policy(self, namespace: str, body: object) -> None:
        self.create_calls.append((namespace, body))

    def delete_namespaced_network_policy(self, name: str, namespace: str) -> None:
        self.delete_calls.append((name, namespace))
        if self.raise_on_delete is not None:
            raise self.raise_on_delete


class KubernetesBackendStub(KubernetesBackend):
    def __init__(self) -> None:
        self.networking_v1_api = FakeNetworkingV1Api()  # pyright: ignore[reportAttributeAccessIssue]


class TestKubernetesChaosClientInit:
    """Test suite for KubernetesChaosClient initialization."""

    def test_init_with_backend(self) -> None:
        # GIVEN a kubernetes backend stub
        stub = KubernetesBackendStub()

        # WHEN initializing the client with the backend
        client = KubernetesChaosClient(backend=stub)

        # THEN the backend is stored
        assert client._backend is stub


class TestIsolateNetwork:
    """Test suite for isolate_network method."""

    def test_creates_network_policy_for_application_label(self) -> None:
        # GIVEN a client wrapping a kubernetes backend stub
        stub = KubernetesBackendStub()
        client = KubernetesChaosClient(backend=stub)

        # WHEN isolating network for a unit
        client.isolate_network(model="test-model", unit="postgresql/0")

        # THEN the policy is created in the model namespace with the expected selector and ingress rules
        assert len(stub.networking_v1_api.create_calls) == 1
        namespace, body = stub.networking_v1_api.create_calls[0]

        assert namespace == "test-model"
        assert body.metadata.name == "chaos-isolate-postgresql"
        assert body.spec.pod_selector.match_labels == {"app.kubernetes.io/name": "postgresql"}
        assert body.spec.policy_types == ["Ingress"]
        assert body.spec.ingress == []


class TestRemoveNetworkIsolation:
    """Test suite for remove_network_isolation method."""

    def test_deletes_network_policy_with_expected_name_and_namespace(self) -> None:
        # GIVEN a client wrapping a kubernetes backend stub
        stub = KubernetesBackendStub()
        client = KubernetesChaosClient(backend=stub)

        # WHEN removing network isolation for a unit
        client.remove_network_isolation(model="test-model", unit="postgresql/0")

        # THEN the policy delete call is issued with the expected policy name and namespace
        assert stub.networking_v1_api.delete_calls == [("chaos-isolate-postgresql", "test-model")]

    def test_ignores_not_found_api_exception(self) -> None:
        # GIVEN a client wrapping a kubernetes backend stub where delete returns 404
        stub = KubernetesBackendStub()
        stub.networking_v1_api.raise_on_delete = ApiException(status=404)
        client = KubernetesChaosClient(backend=stub)

        # WHEN removing network isolation for a unit
        client.remove_network_isolation(model="test-model", unit="postgresql/0")

        # THEN no exception is raised and the delete call was attempted
        assert stub.networking_v1_api.delete_calls == [("chaos-isolate-postgresql", "test-model")]

    def test_reraises_non_404_api_exception(self) -> None:
        # GIVEN a client wrapping a kubernetes backend stub where delete returns 500
        stub = KubernetesBackendStub()
        stub.networking_v1_api.raise_on_delete = ApiException(status=500)
        client = KubernetesChaosClient(backend=stub)

        # WHEN removing network isolation for a unit
        # THEN the API exception is re-raised
        with pytest.raises(ApiException):
            client.remove_network_isolation(model="test-model", unit="postgresql/0")

        assert stub.networking_v1_api.delete_calls == [("chaos-isolate-postgresql", "test-model")]


class TestUnsupportedChaosMethods:
    """Test suite for unsupported chaos methods."""

    def test_fill_disk_raises_not_implemented(self) -> None:
        # GIVEN a client wrapping a kubernetes backend stub
        stub = KubernetesBackendStub()
        client = KubernetesChaosClient(backend=stub)

        # WHEN calling fill_disk
        # THEN it is unsupported for this backend
        with pytest.raises(NotImplementedError):
            client.fill_disk(model=TEST_MODEL, unit="postgresql/0", path="/tmp/fill", size_mb=128)

    def test_stress_cpu_raises_not_implemented(self) -> None:
        # GIVEN a client wrapping a kubernetes backend stub
        stub = KubernetesBackendStub()
        client = KubernetesChaosClient(backend=stub)

        # WHEN calling stress_cpu
        # THEN it is unsupported for this backend
        with pytest.raises(NotImplementedError):
            client.stress_cpu(model=TEST_MODEL, unit="postgresql/0", workers=2, duration=timedelta(seconds=30))

    def test_stress_memory_raises_not_implemented(self) -> None:
        # GIVEN a client wrapping a kubernetes backend stub
        stub = KubernetesBackendStub()
        client = KubernetesChaosClient(backend=stub)

        # WHEN calling stress_memory
        # THEN it is unsupported for this backend
        with pytest.raises(NotImplementedError):
            client.stress_memory(
                model=TEST_MODEL,
                unit="postgresql/0",
                workers=2,
                size_mb=256,
                duration=timedelta(seconds=30),
            )

    def test_cleanup_raises_not_implemented(self) -> None:
        # GIVEN a client wrapping a kubernetes backend stub
        stub = KubernetesBackendStub()
        client = KubernetesChaosClient(backend=stub)

        # WHEN calling cleanup
        # THEN it is unsupported for this backend
        with pytest.raises(NotImplementedError):
            client.cleanup(model=TEST_MODEL, unit="postgresql/0", path="/tmp/fill")
