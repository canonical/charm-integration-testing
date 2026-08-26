# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

from kubernetes import client  # type: ignore[import-untyped]
from kubernetes.client import ApiException  # type: ignore[import-untyped]
from kubernetes_client import KubernetesBackend

from .backend import ChaosClient


class KubernetesChaosClient(ChaosClient):
    def __init__(self, backend: KubernetesBackend):
        self._backend = backend

    def fill_disk(self, model: str, unit: str, path: str, size_mb: int) -> None:
        raise NotImplementedError

    def stress_cpu(self, model: str, unit: str, workers: int, duration: timedelta) -> None:
        raise NotImplementedError

    def stress_memory(self, model: str, unit: str, workers: int, size_mb: int, duration: timedelta) -> None:
        raise NotImplementedError

    def cleanup(self, model: str, unit: str, path: str) -> None:
        raise NotImplementedError

    def isolate_network(self, model: str, unit: str) -> None:
        application = unit.split("/")[0]
        policy = client.V1NetworkPolicy(
            metadata=client.V1ObjectMeta(name=f"chaos-isolate-{application}", namespace=model),
            spec=client.V1NetworkPolicySpec(
                pod_selector=client.V1LabelSelector(match_labels={"app.kubernetes.io/name": application}),
                policy_types=["Ingress"],
                ingress=[],
            ),
        )
        self._backend.networking_v1_api.create_namespaced_network_policy(namespace=model, body=policy)

    def remove_network_isolation(self, model: str, unit: str) -> None:
        application = unit.split("/")[0]
        try:
            self._backend.networking_v1_api.delete_namespaced_network_policy(
                name=f"chaos-isolate-{application}",
                namespace=model,
            )
        except ApiException as error:
            if error.status == 404:
                return
            raise
