# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest
from chaos_client import ChaosCleanupError, MetaChaosClient
from chaos_client.adapters import NetworkIsolationClient
from kubernetes.client import ApiException, V1NetworkPolicy  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend

from .shared import FakeNetworkingV1Api


class BackendStub(KubernetesBackend):
    def __init__(self, api: FakeNetworkingV1Api) -> None:
        self.networking_v1_api = api


class TimeoutApi(FakeNetworkingV1Api):
    def __init__(self, created: bool) -> None:
        super().__init__()
        self.created = created

    def create_namespaced_network_policy(self, namespace: str, body: V1NetworkPolicy) -> None:
        if self.created:
            super().create_namespaced_network_policy(namespace, body)
        raise TimeoutError("Lost create response")


@pytest.mark.parametrize("created", [False, True])
def test_cleanup_reconciles_creation_timeout(created: bool) -> None:
    # GIVEN a create call whose response is lost
    api = TimeoutApi(created)
    tool = MetaChaosClient([NetworkIsolationClient(BackendStub(api))])

    # WHEN execution fails and teardown runs
    with pytest.raises(TimeoutError):
        tool.isolate_network("model", "app/0")
    tool.cleanup_all()

    # THEN only an existing owned policy is deleted, with a UID precondition
    assert api.policies == {}
    assert len(api.delete_calls) == int(created)
    if created:
        options = api.delete_options[0]
        assert options is not None
        assert options.preconditions.uid == "test-policy-uid"


def test_cleanup_preserves_policy_owned_by_another_client() -> None:
    # GIVEN a policy replaced by another owner
    api = FakeNetworkingV1Api()
    backend = BackendStub(api)
    first = NetworkIsolationClient(backend)
    second = NetworkIsolationClient(backend)
    first.isolate_network("model", "app/0")
    second.isolate_network("model", "app/0")

    # WHEN the first client cleans up
    first.remove_network_isolation("model", "app/0")

    # THEN it leaves the replacement for its owner
    assert api.delete_calls == []
    second.remove_network_isolation("model", "app/0")
    assert api.policies == {}


@pytest.mark.parametrize("failure", ["read", "delete"])
def test_cleanup_retries_after_api_failure(failure: str) -> None:
    # GIVEN an owned policy whose cleanup API fails
    api = FakeNetworkingV1Api()
    tool = MetaChaosClient([NetworkIsolationClient(BackendStub(api))])
    tool.isolate_network("model", "app/0")
    error = ApiException(status=500)
    if failure == "read":
        api.raise_on_read = error
    else:
        api.raise_on_delete = error

    # WHEN teardown fails
    with pytest.raises(ChaosCleanupError) as exc_info:
        tool.cleanup_all()
    assert exc_info.value.errors == (error,)

    # THEN the pending policy can be cleaned up on retry
    api.raise_on_read = None
    api.raise_on_delete = None
    tool.cleanup_all()
    assert api.policies == {}


def test_uid_conflict_keeps_replacement_policy_for_retry() -> None:
    # GIVEN a policy replaced between its ownership check and deletion
    api = FakeNetworkingV1Api()
    tool = MetaChaosClient([NetworkIsolationClient(BackendStub(api))])
    tool.isolate_network("model", "app/0")
    api.raise_on_delete = ApiException(status=409)

    # WHEN the API rejects deletion under the old UID
    with pytest.raises(ChaosCleanupError):
        tool.cleanup_all()
    policy = api.policies[("model", "chaos-isolate-app")]
    policy.metadata.annotations = {"charm-integration-testing/owner": "another-owner"}
    api.raise_on_delete = None

    # THEN retry observes the new owner and does not attempt another deletion
    tool.cleanup_all()
    assert len(api.delete_calls) == 1
    assert api.policies[("model", "chaos-isolate-app")] is policy


def test_create_conflict_does_not_delete_existing_policy() -> None:
    # GIVEN a policy owned by another client
    class ConflictApi(FakeNetworkingV1Api):
        def create_namespaced_network_policy(self, namespace: str, body: V1NetworkPolicy) -> None:
            if (namespace, body.metadata.name) in self.policies:
                raise ApiException(status=409)
            super().create_namespaced_network_policy(namespace, body)

    api = ConflictApi()
    backend = BackendStub(api)
    owner = NetworkIsolationClient(backend)
    owner.isolate_network("model", "app/0")
    other = MetaChaosClient([NetworkIsolationClient(backend)])

    # WHEN another client's creation conflicts and teardown runs
    with pytest.raises(ApiException):
        other.isolate_network("model", "app/0")
    other.cleanup_all()

    # THEN the original policy is preserved
    assert api.delete_calls == []
    assert ("model", "chaos-isolate-app") in api.policies
