# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from pathlib import Path

from kubernetes_client.backend import DEFAULT_RETRIES, KubernetesBackend


@dataclass
class LoadKubeConfigStub:
    """Stub standing in for `kubernetes.config.load_kube_config`, recording call args."""

    calls: list[dict[str, str]] = field(default_factory=list)

    def __call__(self, **kwargs: str) -> None:
        self.calls.append(kwargs)


class TestK8sClient:
    def test_configures_shared_retry_policy(self) -> None:
        load_kube_config = LoadKubeConfigStub()

        backend = KubernetesBackend.k8s_client(kubeconfig=Path("/tmp/kubeconfig"), load_kube_config=load_kube_config)

        assert load_kube_config.calls == [{"config_file": "/tmp/kubeconfig"}]
        assert backend.api_client.configuration.retries is DEFAULT_RETRIES

    def test_without_kubeconfig_loads_default_context(self) -> None:
        load_kube_config = LoadKubeConfigStub()

        KubernetesBackend.k8s_client(load_kube_config=load_kube_config)

        assert load_kube_config.calls == [{}]
