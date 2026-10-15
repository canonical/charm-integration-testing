# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from collections.abc import Callable, Mapping
from pathlib import Path

from .backend import KubernetesBackend, KubernetesExtension
from .client import KubernetesClient


class KubernetesClientRegistry:
    """Own and reuse one KubernetesClient for each configured Juju cloud."""

    def __init__(
        self,
        kubeconfigs: Mapping[str, Path],
        client_factory: Callable[[Path], KubernetesClient] | None = None,
        extensions: list[KubernetesExtension] | None = None,
    ) -> None:
        self._kubeconfigs = dict(kubeconfigs)
        self._client_factory = client_factory or self._create_client
        self._clients: dict[str, KubernetesClient] = {}
        self._extensions = list(extensions or [])

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
            client.juju_cloud = cloud
            for extension in self._extensions:
                client.register_extension(extension)
            self._clients[cloud] = client
        return self._clients[cloud]

    def register_extension(self, extension: KubernetesExtension) -> None:
        """Register an extension on existing and future Kubernetes clients."""
        if extension in self._extensions:
            return
        self._extensions.append(extension)
        for client in self._clients.values():
            client.register_extension(extension)

    @staticmethod
    def _create_client(kubeconfig: Path) -> KubernetesClient:
        return KubernetesClient(KubernetesBackend.k8s_client(kubeconfig=kubeconfig))
