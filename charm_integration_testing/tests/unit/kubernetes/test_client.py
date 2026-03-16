# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client import ApiException, V1ObjectMeta, V1Pod, V1PodList, V1PodStatus  # type: ignore[import-untyped]
from kubernetes_client import KubernetesClient


@dataclass
class CoreV1ApiStub:
    """Stub for Kubernetes CoreV1Api client."""

    list_namespaced_pod_result: V1PodList | None = None
    list_namespaced_pod_raises: Exception | None = None
    delete_namespaced_pod_result: Any = None
    delete_namespaced_pod_raises: Exception | None = None
    read_namespaced_pod_result: V1Pod | None = None
    read_namespaced_pod_raises: Exception | None = None

    def list_namespaced_pod(self, namespace: str) -> V1PodList:
        if self.list_namespaced_pod_raises:
            raise self.list_namespaced_pod_raises
        assert self.list_namespaced_pod_result is not None
        return self.list_namespaced_pod_result

    def delete_namespaced_pod(self, name: str, namespace: str) -> Any:
        if self.delete_namespaced_pod_raises:
            raise self.delete_namespaced_pod_raises
        return self.delete_namespaced_pod_result

    def read_namespaced_pod(self, name: str, namespace: str) -> V1Pod:
        if self.read_namespaced_pod_raises:
            raise self.read_namespaced_pod_raises
        assert self.read_namespaced_pod_result is not None
        return self.read_namespaced_pod_result


def create_sample_pod(name: str = "test-pod", namespace: str = "default", phase: str = "Running") -> V1Pod:
    """Helper to create a sample pod for testing."""
    return V1Pod(
        metadata=V1ObjectMeta(name=name, namespace=namespace, uid=f"uid-{name}"),
        status=V1PodStatus(phase=phase),
    )


class TestKubernetesClient:
    """Test suite for KubernetesClient."""

    @patch("kubernetes_client.client.config")
    @patch("kubernetes_client.client.client.CoreV1Api")
    def test_init_with_kubeconfig_path(self, mock_core_v1_api: MagicMock, mock_config: MagicMock) -> None:
        # GIVEN a kubeconfig path
        kubeconfig_path = "/path/to/kubeconfig"

        # WHEN initializing client with path
        client = KubernetesClient(kubeconfig_path=kubeconfig_path)

        # THEN config is loaded with the specified path
        mock_config.load_kube_config.assert_called_once_with(config_file=kubeconfig_path)
        assert client.client is not None

    @patch("kubernetes_client.client.config")
    @patch("kubernetes_client.client.client.CoreV1Api")
    def test_init_without_kubeconfig_path(self, mock_core_v1_api: MagicMock, mock_config: MagicMock) -> None:
        # GIVEN no kubeconfig path
        # WHEN initializing client without path
        client = KubernetesClient()

        # THEN default config is loaded
        mock_config.load_kube_config.assert_called_once_with()
        assert client.client is not None

    @patch("kubernetes_client.client.config")
    @patch("kubernetes_client.client.client.CoreV1Api")
    def test_init_with_logger(self, mock_core_v1_api: MagicMock, mock_config: MagicMock) -> None:
        # GIVEN a custom logger
        logger = logging.getLogger("custom-logger")

        # WHEN initializing client with logger
        client = KubernetesClient(logger=logger)

        # THEN the custom logger is used
        assert client.logger == logger

    @patch("kubernetes_client.client.config")
    @patch("kubernetes_client.client.client.CoreV1Api")
    def test_init_without_logger(self, mock_core_v1_api: MagicMock, mock_config: MagicMock) -> None:
        # GIVEN no custom logger
        # WHEN initializing client without logger
        client = KubernetesClient()

        # THEN a default logger is created
        assert client.logger is not None
        assert isinstance(client.logger, logging.Logger)


class TestListNamespacedPods:
    """Test suite for list_namespaced_pods method."""

    @patch("kubernetes_client.client.config")
    @patch("kubernetes_client.client.client.CoreV1Api")
    def test_success(self, mock_core_v1_api: MagicMock, mock_config: MagicMock) -> None:
        # GIVEN a client with pods in namespace
        pods = [create_sample_pod("pod-1"), create_sample_pod("pod-2")]
        pod_list = V1PodList(items=pods)
        stub = CoreV1ApiStub(list_namespaced_pod_result=pod_list)

        client = KubernetesClient()
        client.client = stub

        # WHEN listing pods
        result = client.list_namespaced_pods("test-namespace")

        # THEN returns list of pods
        assert result == pods
        assert len(result) == 2

    @patch("kubernetes_client.client.config")
    @patch("kubernetes_client.client.client.CoreV1Api")
    def test_empty_namespace(self, mock_core_v1_api: MagicMock, mock_config: MagicMock) -> None:
        # GIVEN a client with empty namespace
        pod_list = V1PodList(items=[])
        stub = CoreV1ApiStub(list_namespaced_pod_result=pod_list)

        client = KubernetesClient()
        client.client = stub

        # WHEN listing pods
        result = client.list_namespaced_pods("empty-namespace")

        # THEN returns empty list
        assert result == []

    @patch("kubernetes_client.client.config")
    @patch("kubernetes_client.client.client.CoreV1Api")
    def test_api_exception(self, mock_core_v1_api: MagicMock, mock_config: MagicMock) -> None:
        # GIVEN a client that raises ApiException
        api_error = ApiException(status=500, reason="Internal Server Error")
        stub = CoreV1ApiStub(list_namespaced_pod_raises=api_error)

        client = KubernetesClient()
        client.client = stub

        # WHEN listing pods
        # THEN raises ApiException
        with pytest.raises(ApiException) as exc_info:
            client.list_namespaced_pods("test-namespace")

        assert exc_info.value.status == 500


class TestListNamespacedPod:
    """Test suite for list_namespaced_pod method."""

    @patch("kubernetes_client.client.config")
    @patch("kubernetes_client.client.client.CoreV1Api")
    def test_success(self, mock_core_v1_api: MagicMock, mock_config: MagicMock) -> None:
        # GIVEN a client with pods in namespace
        pods = [create_sample_pod("pod-1"), create_sample_pod("pod-2")]
        pod_list = V1PodList(items=pods)
        stub = CoreV1ApiStub(list_namespaced_pod_result=pod_list)

        client = KubernetesClient()
        client.client = stub

        # WHEN getting pod list
        result = client.list_namespaced_pod("test-namespace")

        # THEN returns V1PodList
        assert isinstance(result, V1PodList)
        assert result.items == pods

    @patch("kubernetes_client.client.config")
    @patch("kubernetes_client.client.client.CoreV1Api")
    def test_api_exception(self, mock_core_v1_api: MagicMock, mock_config: MagicMock) -> None:
        # GIVEN a client that raises ApiException
        api_error = ApiException(status=403, reason="Forbidden")
        stub = CoreV1ApiStub(list_namespaced_pod_raises=api_error)

        client = KubernetesClient()
        client.client = stub

        # WHEN getting pod list
        # THEN raises ApiException
        with pytest.raises(ApiException) as exc_info:
            client.list_namespaced_pod("test-namespace")

        assert exc_info.value.status == 403


class TestDeletePod:
    """Test suite for delete_pod method."""

    @patch("kubernetes_client.client.config")
    @patch("kubernetes_client.client.client.CoreV1Api")
    def test_success(self, mock_core_v1_api: MagicMock, mock_config: MagicMock) -> None:
        # GIVEN a client with a pod to delete
        stub = CoreV1ApiStub(delete_namespaced_pod_result=None)

        client = KubernetesClient()
        client.client = stub

        # WHEN deleting pod
        client.delete_pod("test-namespace", "test-pod")

        # THEN completes without error

    @patch("kubernetes_client.client.config")
    @patch("kubernetes_client.client.client.CoreV1Api")
    def test_pod_not_found(self, mock_core_v1_api: MagicMock, mock_config: MagicMock) -> None:
        # GIVEN a client where pod doesn't exist
        api_error = ApiException(status=404, reason="Not Found")
        stub = CoreV1ApiStub(delete_namespaced_pod_raises=api_error)

        client = KubernetesClient()
        client.client = stub

        # WHEN deleting non-existent pod
        # THEN raises ApiException
        with pytest.raises(ApiException) as exc_info:
            client.delete_pod("test-namespace", "missing-pod")

        assert exc_info.value.status == 404

    @patch("kubernetes_client.client.config")
    @patch("kubernetes_client.client.client.CoreV1Api")
    def test_api_exception(self, mock_core_v1_api: MagicMock, mock_config: MagicMock) -> None:
        # GIVEN a client that raises ApiException
        api_error = ApiException(status=500, reason="Internal Server Error")
        stub = CoreV1ApiStub(delete_namespaced_pod_raises=api_error)

        client = KubernetesClient()
        client.client = stub

        # WHEN deleting pod
        # THEN raises ApiException
        with pytest.raises(ApiException) as exc_info:
            client.delete_pod("test-namespace", "test-pod")

        assert exc_info.value.status == 500


class TestGetNamespacedPod:
    """Test suite for get_namespaced_pod method."""

    @patch("kubernetes_client.client.config")
    @patch("kubernetes_client.client.client.CoreV1Api")
    def test_success(self, mock_core_v1_api: MagicMock, mock_config: MagicMock) -> None:
        # GIVEN a client with an existing pod
        pod = create_sample_pod("test-pod", "test-namespace")
        stub = CoreV1ApiStub(read_namespaced_pod_result=pod)

        client = KubernetesClient()
        client.client = stub

        # WHEN getting pod
        result = client.get_namespaced_pod("test-pod", "test-namespace")

        # THEN returns the pod
        assert result == pod
        assert result.metadata.name == "test-pod"
        assert result.metadata.namespace == "test-namespace"

    @patch("kubernetes_client.client.config")
    @patch("kubernetes_client.client.client.CoreV1Api")
    def test_pod_not_found(self, mock_core_v1_api: MagicMock, mock_config: MagicMock) -> None:
        # GIVEN a client where pod doesn't exist
        api_error = ApiException(status=404, reason="Not Found")
        stub = CoreV1ApiStub(read_namespaced_pod_raises=api_error)

        client = KubernetesClient()
        client.client = stub

        # WHEN getting non-existent pod
        # THEN raises ApiException
        with pytest.raises(ApiException) as exc_info:
            client.get_namespaced_pod("missing-pod", "test-namespace")

        assert exc_info.value.status == 404

    @patch("kubernetes_client.client.config")
    @patch("kubernetes_client.client.client.CoreV1Api")
    def test_api_exception(self, mock_core_v1_api: MagicMock, mock_config: MagicMock) -> None:
        # GIVEN a client that raises ApiException
        api_error = ApiException(status=401, reason="Unauthorized")
        stub = CoreV1ApiStub(read_namespaced_pod_raises=api_error)

        client = KubernetesClient()
        client.client = stub

        # WHEN getting pod
        # THEN raises ApiException
        with pytest.raises(ApiException) as exc_info:
            client.get_namespaced_pod("test-pod", "test-namespace")

        assert exc_info.value.status == 401
