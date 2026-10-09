# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from copy import deepcopy
from typing import Any
from uuid import uuid4

from kubernetes.client import (  # type: ignore[import-untyped]
    ApiException,
    V1Container,
    V1DeleteOptions,
    V1EnvVar,
    V1NetworkPolicy,
    V1ObjectMeta,
    V1Pod,
    V1PodList,
    V1PodSpec,
)


class FakeCustomObjectsApi:
    def __init__(self, raise_on_delete: ApiException | None = None, *, inject_stress: bool = True) -> None:
        self.inject_stress = inject_stress
        self.create_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, object]] = []
        self.raise_on_delete = raise_on_delete
        self.raise_on_read: ApiException | None = None
        self.objects: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.delete_options: list[V1DeleteOptions | None] = []
        self.request_timeouts: list[tuple[str, float | None]] = []
        self.retain_on_delete = False
        self.stress_status: dict[str, Any] = {
            "conditions": [
                {"type": "Selected", "status": "True"},
                {"type": "AllInjected", "status": "True"},
                {"type": "AllRecovered", "status": "False"},
            ],
            "experiment": {"desiredPhase": "Run", "containerRecords": [{"phase": "Injected"}]},
        }

    def create_namespaced_custom_object(
        self,
        *,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        body: dict[str, object],
        _request_timeout: float | None = None,
    ) -> dict[str, Any]:
        self.request_timeouts.append(("create", _request_timeout))
        self.create_calls.append(
            {"group": group, "version": version, "namespace": namespace, "plural": plural, "body": body}
        )
        stored: dict[str, Any] = deepcopy(body)
        stored["metadata"]["uid"] = uuid4().hex
        if plural == "stresschaos" and self.inject_stress:
            stored["status"] = deepcopy(self.stress_status)
        self.objects[(plural, namespace, stored["metadata"]["name"])] = stored
        return deepcopy(stored)

    def get_namespaced_custom_object(
        self,
        *,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        name: str,
        _request_timeout: float | None = None,
    ) -> dict[str, Any]:
        self.request_timeouts.append(("read", _request_timeout))
        if self.raise_on_read is not None:
            raise self.raise_on_read
        if (plural, namespace, name) not in self.objects:
            raise ApiException(status=404)
        return deepcopy(self.objects[(plural, namespace, name)])

    def delete_namespaced_custom_object(
        self,
        *,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        name: str,
        body: V1DeleteOptions | None = None,
        _request_timeout: float | None = None,
    ) -> None:
        self.request_timeouts.append(("delete", _request_timeout))
        self.delete_calls.append(
            {"group": group, "version": version, "namespace": namespace, "plural": plural, "name": name}
        )
        if self.raise_on_delete is not None:
            if self.raise_on_delete.status == 404:
                self.objects.pop((plural, namespace, name), None)
            raise self.raise_on_delete
        self.delete_options.append(body)
        resource = (plural, namespace, name)
        if resource not in self.objects:
            raise ApiException(status=404)
        if body is not None and body.preconditions.uid != self.objects[resource]["metadata"].get("uid"):
            raise ApiException(status=409)
        if not self.retain_on_delete:
            del self.objects[resource]


class FakeNetworkingV1Api:
    def __init__(self) -> None:
        self.create_calls: list[tuple[str, object]] = []
        self.delete_calls: list[tuple[str, str]] = []
        self.raise_on_delete: Exception | None = None
        self.raise_on_read: Exception | None = None
        self.policies: dict[tuple[str, str], V1NetworkPolicy] = {}
        self.delete_options: list[V1DeleteOptions | None] = []

    def create_namespaced_network_policy(self, namespace: str, body: V1NetworkPolicy) -> None:
        self.create_calls.append((namespace, body))
        body.metadata.uid = "test-policy-uid"
        self.policies[(namespace, body.metadata.name)] = body

    def read_namespaced_network_policy(self, name: str, namespace: str) -> V1NetworkPolicy:
        if self.raise_on_read is not None:
            raise self.raise_on_read
        if (namespace, name) not in self.policies:
            raise ApiException(status=404)
        return self.policies[(namespace, name)]

    def delete_namespaced_network_policy(self, name: str, namespace: str, body: V1DeleteOptions | None = None) -> None:
        self.delete_calls.append((name, namespace))
        self.delete_options.append(body)
        if self.raise_on_delete is not None:
            raise self.raise_on_delete
        self.policies.pop((namespace, name), None)


class FakeWorkloadPodsApi:
    def __init__(self) -> None:
        self.pods = [
            V1Pod(
                metadata=V1ObjectMeta(name="postgresql-pod", annotations={"unit.juju.is/id": "postgresql/0"}),
                spec=V1PodSpec(
                    containers=[
                        V1Container(name="charm"),
                        V1Container(name="postgresql", env=[V1EnvVar(name="JUJU_CONTAINER_NAME", value="postgresql")]),
                    ]
                ),
            )
        ]
        self.calls: list[tuple[str, str, float]] = []

    def list_namespaced_pod(self, namespace: str, label_selector: str, _request_timeout: float) -> V1PodList:
        self.calls.append((namespace, label_selector, _request_timeout))
        return V1PodList(items=self.pods)
