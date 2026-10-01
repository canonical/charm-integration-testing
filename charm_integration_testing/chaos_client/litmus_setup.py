# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass
from functools import partial
from typing import Any, Callable

from kubernetes import client  # type: ignore[import-untyped]
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend

from .litmus_experiments import (
    LitmusExperiment,
    experiment_manifest,
    role_binding_manifest,
    role_manifest,
    service_account_manifest,
)
from .meta_client import ChaosCleanupError

OWNER_ANNOTATION = "charm-integration-testing/owner"
REQUEST_TIMEOUT = (10, 30)


@dataclass
class _Resource:
    kind: str
    read: Callable[..., Any]
    delete: Callable[..., Any]
    uid: str | None = None
    deletion_requested: bool = False


class LitmusSetup:
    """Own the experiment definition and permissions for one execution."""

    def __init__(self, backend: KubernetesBackend, namespace: str, name: str, owner: str) -> None:
        self._backend = backend
        self._namespace = namespace
        self._name = name
        self._owner = owner
        self._pending: list[_Resource] = []
        self._started = False

    def prepare(self, experiment: LitmusExperiment) -> None:
        """Create execution prerequisites, retaining partial failures for cleanup."""
        if self._started:
            raise RuntimeError("Litmus setup cannot be reused for another execution.")
        self._started = True
        core = self._backend.core_v1_api
        rbac = client.RbacAuthorizationV1Api(self._backend.api_client)
        custom = self._backend.custom_objects_api
        self._create(
            service_account_manifest(self._namespace, self._name),
            core.create_namespaced_service_account,
            core.read_namespaced_service_account,
            core.delete_namespaced_service_account,
        )
        self._create(
            role_manifest(self._namespace, self._name),
            rbac.create_namespaced_role,
            rbac.read_namespaced_role,
            rbac.delete_namespaced_role,
        )
        self._create(
            role_binding_manifest(self._namespace, self._name),
            rbac.create_namespaced_role_binding,
            rbac.read_namespaced_role_binding,
            rbac.delete_namespaced_role_binding,
        )
        custom_args = {"group": "litmuschaos.io", "version": "v1alpha1", "plural": "chaosexperiments"}
        self._create(
            experiment_manifest(self._namespace, self._name, experiment),
            partial(custom.create_namespaced_custom_object, **custom_args),
            partial(custom.get_namespaced_custom_object, **custom_args),
            partial(custom.delete_namespaced_custom_object, **custom_args),
        )

    def cleanup(self) -> bool:
        """Request owned resource deletion and report whether it has completed."""
        errors: list[Exception] = []
        for resource in reversed(tuple(self._pending)):
            try:
                if self._remove(resource):
                    self._pending.remove(resource)
            except Exception as error:
                errors.append(error)
        if errors:
            raise ChaosCleanupError(errors) from errors[0]
        return not self._pending

    def _create(
        self,
        body: dict[str, object],
        create: Callable[..., Any],
        read: Callable[..., Any],
        delete: Callable[..., Any],
    ) -> None:
        body = {
            **body,
            "metadata": {
                "name": self._name,
                "namespace": self._namespace,
                "annotations": {OWNER_ANNOTATION: self._owner},
            },
        }
        resource = _Resource(
            kind=str(body["kind"]),
            read=partial(read, namespace=self._namespace, name=self._name, _request_timeout=REQUEST_TIMEOUT),
            delete=partial(delete, namespace=self._namespace, name=self._name, _request_timeout=REQUEST_TIMEOUT),
        )
        # A lost response does not mean that creation failed on the server.
        self._pending.append(resource)
        try:
            created = create(namespace=self._namespace, body=body, _request_timeout=REQUEST_TIMEOUT)
        except ApiException as error:
            if error.status == 409:
                self._pending.remove(resource)
            raise
        resource.uid = self._metadata(created).get("uid")

    def _remove(self, resource: _Resource) -> bool:
        try:
            metadata = self._metadata(resource.read())
        except ApiException as error:
            if error.status == 404:
                return True
            raise
        uid = metadata.get("uid")
        if not uid or (metadata.get("annotations") or {}).get(OWNER_ANNOTATION) != self._owner:
            raise RuntimeError(f"Cannot verify ownership of {resource.kind} {self._namespace}/{self._name}.")
        if resource.uid is None:
            raise RuntimeError(
                f"Creation UID was not recorded for {resource.kind} {self._namespace}/{self._name}; cleanup retained."
            )
        if resource.uid != uid:
            raise RuntimeError(f"{resource.kind} {self._namespace}/{self._name} was replaced.")
        if not resource.deletion_requested:
            try:
                resource.delete(body=client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=uid)))
            except ApiException as error:
                if error.status == 404:
                    return True
                raise
            resource.deletion_requested = True
        # Keep tracking until a later GET confirms deletion.
        return False

    @staticmethod
    def _metadata(resource: Any) -> dict[str, Any]:
        if isinstance(resource, dict):
            metadata: dict[str, Any] = resource["metadata"]
        else:
            metadata = resource.metadata.to_dict()
        return metadata
