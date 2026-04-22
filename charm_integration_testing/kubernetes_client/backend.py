# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


from pathlib import Path

from kubernetes import client, config  # type: ignore[import-untyped]


class KubernetesBackend:
    core_v1_api: client.CoreV1Api
    apps_v1_api: client.AppsV1Api
    api_client: client.ApiClient

    def __init__(self, api_client: client.ApiClient):
        self.api_client = api_client

        # Instantiate the used API groups
        self.core_v1_api = client.CoreV1Api(api_client)
        self.apps_v1_api = client.AppsV1Api(api_client)

    @classmethod
    def k8s_client(cls, kubeconfig: Path | None = None) -> "KubernetesBackend":
        if kubeconfig:
            config.load_kube_config(config_file=str(kubeconfig.resolve()))
        else:
            config.load_kube_config()

        return cls(client.ApiClient())
