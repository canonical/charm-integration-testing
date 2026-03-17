# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


from kubernetes import client, config  # type: ignore[import-untyped]


class KubernetesClient:
    @staticmethod
    def k8s_client(kubeconfig: str | None = None) -> client.CoreV1Api:
        if kubeconfig:
            config.load_kube_config(config_file=kubeconfig)
        else:
            config.load_kube_config()
        return client.CoreV1Api()
