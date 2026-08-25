# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


from abc import ABC
from pathlib import Path
from types import MappingProxyType
from typing import Callable

from kubernetes import client, config  # type: ignore[import-untyped]
from urllib3.util import Retry

# Template retry policy for a single ApiClient's Configuration (see k8s_client). Every request
# made through any API group (including ad-hoc ones instantiated directly against `api_client`)
# gets the same retry behaviour without each call site needing its own retry/backoff logic.
DEFAULT_RETRY_KWARGS: MappingProxyType[str, object] = MappingProxyType(
    {
        "total": 5,
        "backoff_factor": 1,
        "status_forcelist": frozenset([500, 502, 503, 504]),
        # POST is excluded: it's not idempotent, and retrying it risks duplicate side effects if
        # the server actually processed the original request before the client saw a disconnect.
        "allowed_methods": frozenset(["HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE", "PATCH"]),
        # Let the Kubernetes client see the final 5xx response (and raise its usual ApiException)
        # once retries are exhausted, instead of urllib3 raising MaxRetryError.
        "raise_on_status": False,
    }
)


def build_default_retries() -> Retry:
    """Builds a fresh `Retry` instance from `DEFAULT_RETRY_KWARGS`.

    Returns a new instance on every call, so no two clients (or anything else in the process)
    ever share the same mutable `Retry` object.
    """
    return Retry(**DEFAULT_RETRY_KWARGS)  # type: ignore[arg-type]


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
    def k8s_client(
        cls,
        kubeconfig: Path | None = None,
        *,
        load_kube_config: Callable[..., None] = config.load_kube_config,
    ) -> "KubernetesBackend":
        if kubeconfig:
            load_kube_config(config_file=str(kubeconfig.resolve()))
        else:
            load_kube_config()

        configuration = client.Configuration.get_default_copy()
        configuration.retries = build_default_retries()
        return cls(client.ApiClient(configuration))
