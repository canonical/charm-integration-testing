# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


from kubernetes import client, config  # type: ignore[import-untyped]


class KubernetesBackend:
    corev1_api: client.CoreV1Api
    appsv1_api: client.AppsV1Api
    api_client: client.ApiClient

    def __init__(self, api_client: client.ApiClient):
        self.api_client = api_client

        # Instantiate the used API groups
        self.corev1_api = client.CoreV1Api(api_client)
        self.appsv1_api = client.AppsV1Api(api_client)

    @classmethod
    def k8s_client(cls, kubeconfig: str | None = None) -> "KubernetesBackend":
        if kubeconfig:
            config.load_kube_config(config_file=kubeconfig)
        else:
            config.load_kube_config()

        return cls(client.ApiClient())
