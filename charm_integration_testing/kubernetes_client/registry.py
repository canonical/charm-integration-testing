# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from collections.abc import Callable, Mapping
from pathlib import Path

from .backend import KubernetesBackend
from .client import KubernetesClient


class KubernetesClientRegistry:
    """Own and reuse one KubernetesClient for each configured Juju cloud."""

    def __init__(
        self,
        kubeconfigs: Mapping[str, Path],
        client_factory: Callable[[Path], KubernetesClient] | None = None,
    ) -> None:
        self._kubeconfigs = dict(kubeconfigs)
        self._client_factory = client_factory or self._create_client
        self._clients: dict[str, KubernetesClient] = {}

    def has_cloud(self, cloud: str) -> bool:
        return cloud in self._kubeconfigs

    def kubeconfig_for_cloud(self, cloud: str) -> Path:
        try:
            return self._kubeconfigs[cloud]
        except KeyError as exc:
            raise ValueError(f"No kubeconfig configured for Kubernetes cloud '{cloud}'") from exc

    def client_for_cloud(self, cloud: str) -> KubernetesClient:
        if cloud not in self._clients:
            client = self._client_factory(self.kubeconfig_for_cloud(cloud))
            self._clients[cloud] = client
        return self._clients[cloud]

    @staticmethod
    def _create_client(kubeconfig: Path) -> KubernetesClient:
        return KubernetesClient(KubernetesBackend.k8s_client(kubeconfig=kubeconfig))
