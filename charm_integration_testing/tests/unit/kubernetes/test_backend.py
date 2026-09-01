# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from kubernetes.client import ApiException  # type: ignore[import-untyped]
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


class FakeApiextensionsV1Api:
    """Records read_custom_resource_definition calls and optionally raises."""

    def __init__(self, error: ApiException | None = None) -> None:
        self.error = error
        self.reads: list[str] = []

    def read_custom_resource_definition(self, name: str) -> object:
        self.reads.append(name)
        if self.error is not None:
            raise self.error
        return object()


class BackendWithFakeApiextensions(KubernetesBackend):
    def __init__(self, apiextensions: FakeApiextensionsV1Api) -> None:
        self.apiextensions_v1_api = apiextensions


class TestCrdExists:
    def test_true_when_crd_is_registered(self) -> None:
        fake = FakeApiextensionsV1Api()
        backend = BackendWithFakeApiextensions(fake)

        assert backend.crd_exists("stresschaos.chaos-mesh.org") is True
        assert fake.reads == ["stresschaos.chaos-mesh.org"]

    def test_false_when_crd_is_absent(self) -> None:
        backend = BackendWithFakeApiextensions(FakeApiextensionsV1Api(ApiException(status=404)))

        assert backend.crd_exists("missing.example.com") is False

    def test_reraises_non_404_api_exception(self) -> None:
        backend = BackendWithFakeApiextensions(FakeApiextensionsV1Api(ApiException(status=500)))

        with pytest.raises(ApiException):
            backend.crd_exists("boom.example.com")
