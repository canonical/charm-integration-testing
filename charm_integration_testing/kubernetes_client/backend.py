# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


from abc import ABC
from pathlib import Path

from kubernetes import client, config  # type: ignore[import-untyped]
from urllib3.util import Retry

# Applied to the shared ApiClient's connection pool, so every request made through any API
# group (including ad-hoc ones instantiated directly against `api_client`) gets the same
# retry behaviour without each call site needing its own retry/backoff logic.
DEFAULT_RETRIES = Retry(
    total=5,
    backoff_factor=1,
    status_forcelist=[500, 502, 503, 504],
    allowed_methods=None,  # retry on all methods, including PATCH/POST/PUT/DELETE
)


class KubernetesExtension(ABC):
    def post_delete_pod(self, namespace: str, pod_name: str) -> None:
        pass

    def post_restart_statefulset(self, namespace: str, statefulset_name: str) -> None:
        pass


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

        configuration = client.Configuration.get_default_copy()
        configuration.retries = DEFAULT_RETRIES
        return cls(client.ApiClient(configuration))
