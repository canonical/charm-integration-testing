# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from pathlib import Path

from kubernetes_client.backend import DEFAULT_RETRY_KWARGS, KubernetesBackend
from urllib3.util import Retry


@dataclass
class LoadKubeConfigStub:
    """Stub standing in for `kubernetes.config.load_kube_config`, recording call args."""

    calls: list[dict[str, object]] = field(default_factory=list)

    def __call__(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


class TestK8sClient:
    def test_configures_shared_retry_policy(self, tmp_path: Path) -> None:
        kubeconfig = tmp_path / "kubeconfig"
        load_kube_config = LoadKubeConfigStub()

        backend = KubernetesBackend.k8s_client(kubeconfig=kubeconfig, load_kube_config=load_kube_config)

        assert load_kube_config.calls == [{"config_file": str(kubeconfig.resolve())}]
        retries = backend.api_client.configuration.retries
        assert isinstance(retries, Retry)
        assert retries.total == DEFAULT_RETRY_KWARGS["total"]
        assert retries.backoff_factor == DEFAULT_RETRY_KWARGS["backoff_factor"]
        assert retries.status_forcelist == DEFAULT_RETRY_KWARGS["status_forcelist"]
        assert retries.allowed_methods == DEFAULT_RETRY_KWARGS["allowed_methods"]
        assert retries.raise_on_status == DEFAULT_RETRY_KWARGS["raise_on_status"]

    def test_without_kubeconfig_loads_default_context(self) -> None:
        load_kube_config = LoadKubeConfigStub()

        KubernetesBackend.k8s_client(load_kube_config=load_kube_config)

        assert load_kube_config.calls == [{}]

    def test_each_client_gets_its_own_retries_instance(self) -> None:
        first = KubernetesBackend.k8s_client(load_kube_config=LoadKubeConfigStub())
        second = KubernetesBackend.k8s_client(load_kube_config=LoadKubeConfigStub())

        assert first.api_client.configuration.retries is not second.api_client.configuration.retries
