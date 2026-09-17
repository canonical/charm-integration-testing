# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest
from kubernetes import client  # type: ignore[import-untyped]
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

    @pytest.mark.parametrize("status", [401, 403, 500])
    def test_reraises_non_404_api_exception(self, status: int) -> None:
        backend = BackendWithFakeApiextensions(FakeApiextensionsV1Api(ApiException(status=status)))

        with pytest.raises(ApiException):
            backend.crd_exists("boom.example.com")


class FakeAppsV1Api:
    def __init__(self, deployment: client.V1Deployment | None = None, error: ApiException | None = None) -> None:
        self.deployment = deployment
        self.error = error
        self.reads: list[tuple[str, str]] = []

    def read_namespaced_deployment(self, *, name: str, namespace: str) -> client.V1Deployment:
        self.reads.append((namespace, name))
        if self.error is not None:
            raise self.error
        assert self.deployment is not None
        return self.deployment


class BackendWithFakeApps(KubernetesBackend):
    def __init__(self, apps: FakeAppsV1Api) -> None:
        self.apps_v1_api = apps


class TestDeploymentIsReady:
    @dataclass(frozen=True)
    class Params:
        label: str
        expected: bool = False
        replicas: int | None = 2
        generation: int | None = 2
        observed_generation: int | None = 2
        updated: int | None = 2
        ready: int | None = 2
        available: int | None = 2
        deleting: bool = False

    test_cases = [
        Params(label="ready", expected=True),
        Params(label="missing-generation", generation=None),
        Params(label="stale-generation", observed_generation=1),
        Params(label="unobserved-generation", observed_generation=None),
        Params(label="partially-updated", updated=1),
        Params(label="partially-ready", ready=1),
        Params(label="partially-available", available=1),
        Params(label="missing-updated-count", updated=None),
        Params(label="missing-ready-count", ready=None),
        Params(label="missing-available-count", available=None),
        Params(label="scaled-to-zero", replicas=0, updated=0, ready=0, available=0),
        Params(label="default-replicas", expected=True, replicas=None, updated=1, ready=1, available=1),
        Params(label="deleting", deleting=True),
    ]

    @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
    def test_checks_current_rollout(self, params: Params) -> None:
        # GIVEN a Deployment with the specified rollout status
        deployment = client.V1Deployment(
            metadata=client.V1ObjectMeta(
                generation=params.generation,
                deletion_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) if params.deleting else None,
            ),
            spec=client.V1DeploymentSpec(
                replicas=params.replicas,
                selector=client.V1LabelSelector(match_labels={"app": "operator"}),
                template=client.V1PodTemplateSpec(),
            ),
            status=client.V1DeploymentStatus(
                observed_generation=params.observed_generation,
                updated_replicas=params.updated,
                ready_replicas=params.ready,
                available_replicas=params.available,
            ),
        )
        apps = FakeAppsV1Api(deployment)
        backend = BackendWithFakeApps(apps)

        # WHEN checking Deployment readiness
        result = backend.deployment_is_ready("execution-model", "operator")

        # THEN readiness matches the expected result in the requested namespace
        assert result is params.expected
        assert apps.reads == [("execution-model", "operator")]

    @pytest.mark.parametrize("missing_field", ["metadata", "spec", "status"])
    def test_false_when_deployment_details_are_missing(self, missing_field: str) -> None:
        # GIVEN a Deployment missing metadata, spec or status
        deployment = client.V1Deployment(
            metadata=client.V1ObjectMeta(generation=1),
            spec=client.V1DeploymentSpec(selector=client.V1LabelSelector(), template=client.V1PodTemplateSpec()),
            status=client.V1DeploymentStatus(
                observed_generation=1, updated_replicas=1, ready_replicas=1, available_replicas=1
            ),
        )
        setattr(deployment, missing_field, None)
        backend = BackendWithFakeApps(FakeAppsV1Api(deployment))

        # WHEN checking readiness
        result = backend.deployment_is_ready("execution-model", "operator")

        # THEN the Deployment is not ready
        assert result is False

    def test_false_when_deployment_is_absent(self) -> None:
        # GIVEN a missing Deployment
        backend = BackendWithFakeApps(FakeAppsV1Api(error=ApiException(status=404)))

        # WHEN checking readiness
        result = backend.deployment_is_ready("execution-model", "operator")

        # THEN the Deployment is not ready
        assert result is False

    @pytest.mark.parametrize("status", [401, 403, 500])
    def test_reraises_non_404_api_exception(self, status: int) -> None:
        # GIVEN a non-404 API error
        error = ApiException(status=status)
        backend = BackendWithFakeApps(FakeAppsV1Api(error=error))

        # WHEN checking readiness
        # THEN the original exception is re-raised
        with pytest.raises(ApiException) as exc_info:
            backend.deployment_is_ready("execution-model", "operator")

        assert exc_info.value is error
