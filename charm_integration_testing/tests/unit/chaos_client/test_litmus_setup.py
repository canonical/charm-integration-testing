# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from chaos_client import ChaosCleanupError
from chaos_client.litmus_setup import OWNER_ANNOTATION, LitmusSetup
from kubernetes import client  # type: ignore[import-untyped]
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend


class ResourceStore:
    def __init__(self) -> None:
        self.resources: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.create_failure: tuple[str, Exception, bool] | None = None
        self.delete_failure: str | None = None
        self.replace_on_delete: str | None = None
        self.hold_deletions = False

    def create(self, kind: str, *, namespace: str, body: dict[str, Any], **kwargs: Any) -> Any:
        self.created.append(kind)
        failure = self.create_failure
        if failure is not None and failure[0] == kind and not failure[2]:
            raise failure[1]
        resource = deepcopy(body)
        resource["metadata"]["uid"] = f"uid-{kind}-{len(self.created)}"
        self.resources[kind, namespace, resource["metadata"]["name"]] = resource
        if failure is not None and failure[0] == kind:
            raise failure[1]
        return self._response(kind, resource)

    def read(self, kind: str, *, namespace: str, name: str, **kwargs: Any) -> Any:
        resource = self.resources.get((kind, namespace, name))
        if resource is None:
            raise ApiException(status=404)
        return self._response(kind, resource)

    def delete(self, kind: str, *, namespace: str, name: str, body: Any, **kwargs: Any) -> None:
        if self.delete_failure == kind:
            raise ApiException(status=503)
        key = (kind, namespace, name)
        resource = self.resources.get(key)
        if resource is None:
            raise ApiException(status=404)
        if self.replace_on_delete == kind:
            resource["metadata"]["uid"] = "replacement"
        if body.preconditions.uid != resource["metadata"]["uid"]:
            raise ApiException(status=409)
        self.deleted.append(kind)
        if not self.hold_deletions:
            del self.resources[key]

    @staticmethod
    def _response(kind: str, resource: dict[str, Any]) -> Any:
        if kind == "experiment":
            return deepcopy(resource)
        return SimpleNamespace(metadata=client.V1ObjectMeta(**resource["metadata"]))


class BackendStub(KubernetesBackend):
    def __init__(self) -> None:
        self.api_client = MagicMock()
        self.core_v1_api = MagicMock(spec=client.CoreV1Api)
        self.custom_objects_api = MagicMock(spec=client.CustomObjectsApi)


@dataclass
class SetupContext:
    backend: BackendStub
    store: ResourceStore

    def setup(self, name: str = "cit-test", owner: str = "owner") -> LitmusSetup:
        return LitmusSetup(self.backend, "model", name, owner)


@pytest.fixture
def context(monkeypatch: pytest.MonkeyPatch) -> SetupContext:
    backend = BackendStub()
    store = ResourceStore()
    rbac = MagicMock(spec=client.RbacAuthorizationV1Api)
    monkeypatch.setattr(client, "RbacAuthorizationV1Api", lambda _: rbac)
    for api, kind, suffix in [
        (backend.core_v1_api, "account", "service_account"),
        (rbac, "role", "role"),
        (rbac, "binding", "role_binding"),
        (backend.custom_objects_api, "experiment", "custom_object"),
    ]:
        getattr(api, f"create_namespaced_{suffix}").side_effect = partial(store.create, kind)
        read = "get" if kind == "experiment" else "read"
        getattr(api, f"{read}_namespaced_{suffix}").side_effect = partial(store.read, kind)
        getattr(api, f"delete_namespaced_{suffix}").side_effect = partial(store.delete, kind)
    return SetupContext(backend, store)


def test_setup_cleans_up_only_its_own_execution(context: SetupContext) -> None:
    # GIVEN two prepared executions in the same namespace
    first = context.setup("first", "first-owner")
    second = context.setup("second", "second-owner")
    first.prepare("pod-cpu-hog")
    second.prepare("pod-memory-hog")

    # WHEN deleting the first execution and confirming deletion
    assert not first.cleanup()
    assert first.cleanup()

    # THEN all four resources of the other execution remain
    assert len(context.store.resources) == 4
    assert {key[2] for key in context.store.resources} == {"second"}
    assert context.store.deleted == ["experiment", "binding", "role", "account"]
    assert not second.cleanup()
    assert second.cleanup()
    assert context.store.resources == {}


@pytest.mark.parametrize("kind", ["account", "role", "binding", "experiment"])
@pytest.mark.parametrize("created", [False, True], ids=["not-created", "response-lost"])
def test_partial_preparation_failure_remains_cleanable(context: SetupContext, kind: str, created: bool) -> None:
    # GIVEN a failed create at any preparation stage
    setup = context.setup()
    failure = TimeoutError("Create response lost")
    context.store.create_failure = (kind, failure, created)

    # WHEN preparation fails
    with pytest.raises(TimeoutError) as exc_info:
        setup.prepare("pod-cpu-hog")

    # THEN the original failure propagates and partial resources can be removed
    assert exc_info.value is failure
    setup.cleanup()
    assert setup.cleanup()
    assert context.store.resources == {}


def test_create_conflict_preserves_existing_resource(context: SetupContext) -> None:
    # GIVEN a foreign RoleBinding with the requested name
    key = ("binding", "model", "cit-test")
    foreign = {"metadata": {"name": "cit-test", "uid": "foreign", "annotations": {OWNER_ANNOTATION: "other"}}}
    context.store.resources[key] = foreign
    context.store.create_failure = ("binding", ApiException(status=409), False)
    setup = context.setup()

    # WHEN creation conflicts after creating the account and role
    with pytest.raises(ApiException):
        setup.prepare("pod-cpu-hog")
    setup.cleanup()
    assert setup.cleanup()

    # THEN previously created resources are removed and the conflicting binding remains
    assert context.store.resources == {key: foreign}
    assert "binding" not in context.store.deleted


@pytest.mark.parametrize("field", ["owner", "uid"])
def test_replacement_resource_is_not_deleted(context: SetupContext, field: str) -> None:
    # GIVEN a prepared resource whose ownership or identity changes
    setup = context.setup()
    setup.prepare("pod-cpu-hog")
    key = ("experiment", "model", "cit-test")
    metadata = context.store.resources[key]["metadata"]
    if field == "owner":
        metadata["annotations"][OWNER_ANNOTATION] = "other"
    else:
        metadata["uid"] = "replacement"

    # WHEN cleaning up, THEN the changed resource is retained and the failure is reported
    with pytest.raises(ChaosCleanupError):
        setup.cleanup()
    assert key in context.store.resources
    assert "experiment" not in context.store.deleted
    assert set(context.store.deleted) == {"binding", "role", "account"}


def test_cleanup_continues_after_delete_failure_and_retries(context: SetupContext) -> None:
    # GIVEN a transient error deleting the experiment definition
    setup = context.setup()
    setup.prepare("pod-cpu-hog")
    context.store.delete_failure = "experiment"

    # WHEN cleanup fails, THEN other resources are still removed
    with pytest.raises(ChaosCleanupError):
        setup.cleanup()
    assert set(context.store.deleted) == {"binding", "role", "account"}

    # WHEN the API recovers, THEN a retry finishes cleanup
    context.store.delete_failure = None
    assert not setup.cleanup()
    assert setup.cleanup()
    assert context.store.resources == {}


def test_cleanup_waits_for_deletion_confirmation(context: SetupContext) -> None:
    # GIVEN accepted DELETE requests with resources still present
    setup = context.setup()
    setup.prepare("pod-cpu-hog")
    context.store.hold_deletions = True

    # WHEN checking cleanup twice
    assert not setup.cleanup()
    assert not setup.cleanup()

    # THEN deletion is requested once and completion waits for actual absence
    assert len(context.store.deleted) == 4
    context.store.resources.clear()
    assert setup.cleanup()


def test_uid_precondition_protects_replacement_between_read_and_delete(context: SetupContext) -> None:
    # GIVEN a resource that is replaced immediately after the ownership read
    setup = context.setup()
    setup.prepare("pod-cpu-hog")
    context.store.replace_on_delete = "experiment"

    # WHEN attempting deletion, THEN the API rejects the stale UID
    with pytest.raises(ChaosCleanupError) as exc_info:
        setup.cleanup()
    error = exc_info.value.errors[0]
    assert isinstance(error, ApiException)
    assert error.status == 409
    assert "experiment" not in context.store.deleted
    resource = context.store.resources["experiment", "model", "cit-test"]
    assert resource["metadata"]["uid"] == "replacement"


def test_preparation_cannot_be_repeated(context: SetupContext) -> None:
    # GIVEN a prepared execution
    setup = context.setup()
    setup.prepare("pod-cpu-hog")

    # WHEN trying to reuse it, THEN no second set of resources is created
    with pytest.raises(RuntimeError, match="cannot be reused"):
        setup.prepare("pod-memory-hog")
    assert len(context.store.created) == 4
