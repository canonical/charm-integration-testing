# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path
from unittest.mock import MagicMock, patch

from kubernetes_client.backend import DEFAULT_RETRIES, KubernetesBackend


class TestK8sClient:
    @patch("kubernetes_client.backend.client.ApiClient")
    @patch("kubernetes_client.backend.client.Configuration.get_default_copy")
    @patch("kubernetes_client.backend.config.load_kube_config")
    def test_configures_shared_retry_policy(
        self, mock_load_kube_config: MagicMock, mock_get_default_copy: MagicMock, mock_api_client: MagicMock
    ) -> None:
        configuration = MagicMock()
        mock_get_default_copy.return_value = configuration

        KubernetesBackend.k8s_client(kubeconfig=Path("/tmp/kubeconfig"))

        mock_load_kube_config.assert_called_once_with(config_file="/tmp/kubeconfig")
        assert configuration.retries is DEFAULT_RETRIES
        mock_api_client.assert_called_once_with(configuration)

    @patch("kubernetes_client.backend.client.ApiClient")
    @patch("kubernetes_client.backend.client.Configuration.get_default_copy")
    @patch("kubernetes_client.backend.config.load_kube_config")
    def test_without_kubeconfig_loads_default_context(
        self, mock_load_kube_config: MagicMock, mock_get_default_copy: MagicMock, mock_api_client: MagicMock
    ) -> None:
        KubernetesBackend.k8s_client()

        mock_load_kube_config.assert_called_once_with()
