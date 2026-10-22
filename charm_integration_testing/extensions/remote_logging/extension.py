# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass
from typing import Protocol

from juju import JujuBackend, JujuExtension, JujuModelHandle
from juju.handles import JujuControllerHandle
from resource_registry import ResourceRegistry, sanitize_path_segment

from .scraper import (
    KUBERNETES_NAMESPACE_PREFIX,
    KUBERNETES_NAMESPACES_KEY,
    KUBERNETES_SCRAPER_NAME,
    USERDATA_KEY,
    kubernetes_config_map,
    kubernetes_daemon_set,
    machine_userdata,
    merge_userdata,
    safe_controller_name,
)

CONTROLLER_MODEL = "controller"


class KubernetesScraperClient(Protocol):
    """Kubernetes operations required to manage a remote logging scraper."""

    def ensure_namespace(self, name: str) -> None: ...

    def delete_namespace(self, name: str) -> None: ...

    def read_config_map_data(self, namespace: str, name: str) -> dict[str, str] | None: ...

    def apply_config_map(self, namespace: str, body: dict[str, object]) -> None: ...

    def apply_daemon_set(self, namespace: str, body: dict[str, object]) -> None: ...

    def restart_daemon_set(self, namespace: str, name: str) -> None: ...


class KubernetesClientResolver(Protocol):
    """Resolve shared Kubernetes clients by cloud."""

    def has_cloud(self, cloud: str) -> bool: ...

    def client_for_cloud(self, cloud: str) -> KubernetesScraperClient: ...


def scraper_namespace(controller: str) -> str:
    return f"{KUBERNETES_NAMESPACE_PREFIX}{safe_controller_name(controller)}"


def controller_namespace(controller: str) -> str:
    return f"controller-{controller}"


@dataclass(frozen=True)
class LogScraperHandle:
    """Registry handle of the log scraper deployed for a Kubernetes controller."""

    controller: str
    cloud: str | None = None

    @property
    def resource_id(self) -> str:
        suffix = f":{self.cloud}" if self.cloud else ""
        return f"remote-logging:scraper:{self.controller}{suffix}"

    @property
    def resource_type(self) -> str:
        return "remote-logging:scraper"

    @property
    def path_segment(self) -> str:
        suffix = f"-{self.cloud}" if self.cloud else ""
        safe = sanitize_path_segment(f"{self.controller}{suffix}")
        return f"remote-logging-scraper-{safe}"


class RemoteLoggingExtension(JujuExtension):
    """Ship the logs of a controller and its models to a remote Loki endpoint.

    Machine controllers and models get a collector installed on every machine through the
    ``cloudinit-userdata`` model config. Kubernetes controllers get a single DaemonSet scraping the pod logs of
    the controller and model namespaces.
    """

    def __init__(
        self,
        juju: JujuBackend,
        kubernetes_client_registry: KubernetesClientResolver,
        endpoints_by_controller: dict[str, str],
        resource_registry: ResourceRegistry,
    ) -> None:
        self.juju = juju
        self.kubernetes_client_registry = kubernetes_client_registry
        self.endpoints_by_controller = endpoints_by_controller
        self.resource_registry = resource_registry

    def _kubernetes_client_for_model(self, model: JujuModelHandle) -> KubernetesScraperClient | None:
        cloud = self.juju.kubernetes_cloud_for_model(model)
        if cloud is None:
            return None
        if not self.kubernetes_client_registry.has_cloud(cloud):
            raise RuntimeError(f"No kubeconfig configured for Kubernetes cloud '{cloud}' (model {model.uri}).")
        return self.kubernetes_client_registry.client_for_cloud(cloud)

    def _kubernetes_client_for_controller(self, controller: str) -> KubernetesScraperClient | None:
        return self._kubernetes_client_for_model(JujuModelHandle(controller=controller, model=CONTROLLER_MODEL))

    def pre_bootstrap_controller(
        self, cloud: str, controller: str, bootstrap_configuration: dict[str, str]
    ) -> dict[str, str]:
        endpoint = self.endpoints_by_controller.get(controller)
        if endpoint is None or self.juju.is_k8s_cloud(cloud):
            return bootstrap_configuration
        return self._with_userdata(bootstrap_configuration, endpoint, controller, CONTROLLER_MODEL)

    def post_bootstrap_controller(self, controller: str) -> None:
        endpoint = self.endpoints_by_controller.get(controller)
        if endpoint is None:
            return

        kubernetes = self._kubernetes_client_for_controller(controller)
        if kubernetes is None:
            return
        self._deploy_scraper(
            kubernetes,
            endpoint,
            controller,
            [controller_namespace(controller)],
            LogScraperHandle(controller),
        )

    def pre_add_model(
        self, controller: str, model: str, model_config: dict[str, str], cloud: str | None
    ) -> dict[str, str]:
        endpoint = self.endpoints_by_controller.get(controller)
        if endpoint is None:
            return model_config

        is_kubernetes = (
            self.juju.is_k8s_cloud_on_controller(cloud, controller)
            if cloud is not None
            else self._kubernetes_client_for_controller(controller) is not None
        )
        if is_kubernetes:
            return model_config

        return self._with_userdata(model_config, endpoint, controller, model)

    def post_add_model_with_cloud(self, controller: str, model: str, cloud: str | None) -> None:
        endpoint = self.endpoints_by_controller.get(controller)
        if endpoint is None:
            return

        kubernetes = self._kubernetes_client_for_model(JujuModelHandle(controller=controller, model=model))
        if kubernetes is None:
            return
        self._ensure_scraper(kubernetes, endpoint, controller, model, cloud)

    @staticmethod
    def _with_userdata(config: dict[str, str], endpoint: str, controller: str, model: str) -> dict[str, str]:
        userdata = merge_userdata(config.get(USERDATA_KEY), machine_userdata(endpoint, controller, model))
        return {**config, USERDATA_KEY: userdata}

    def _scrape_namespace(
        self,
        kubernetes: KubernetesScraperClient,
        endpoint: str,
        controller: str,
        model: str,
        data: dict[str, str],
    ) -> bool:
        """Reconcile the ConfigMap and report whether a running scraper needs restarting."""
        namespace = scraper_namespace(controller)
        scraped = [name for name in data.get(KUBERNETES_NAMESPACES_KEY, "").split("\n") if name]
        if not scraped and self._kubernetes_client_for_controller(controller) is kubernetes:
            controller_model_namespace = controller_namespace(controller)
            scraped.append(controller_model_namespace)
        if model not in scraped:
            scraped.append(model)
        changed = self._reconcile_config_map(kubernetes, namespace, endpoint, controller, scraped, data)
        return changed

    @staticmethod
    def _reconcile_config_map(
        kubernetes: KubernetesScraperClient,
        namespace: str,
        endpoint: str,
        controller: str,
        scraped: list[str],
        existing_data: dict[str, str] | None,
    ) -> bool:
        desired_config_map = kubernetes_config_map(namespace, endpoint, controller, scraped)
        desired_data = desired_config_map["data"]
        changed = existing_data is not None and any(
            existing_data.get(key) != value for key, value in desired_data.items()
        )
        if existing_data is None or changed:
            kubernetes.apply_config_map(namespace, desired_config_map)
        return changed

    def _ensure_scraper(
        self,
        kubernetes: KubernetesScraperClient,
        endpoint: str,
        controller: str,
        model: str,
        cloud: str | None,
    ) -> None:
        handle = (
            LogScraperHandle(controller)
            if self._kubernetes_client_for_controller(controller) is kubernetes
            else LogScraperHandle(controller, cloud)
        )
        self._register_scraper(kubernetes, controller, handle)
        namespace = scraper_namespace(controller)
        data = kubernetes.read_config_map_data(namespace, KUBERNETES_SCRAPER_NAME)
        if data is not None:
            kubernetes.apply_daemon_set(namespace, kubernetes_daemon_set(namespace, controller))
            if self._scrape_namespace(kubernetes, endpoint, controller, model, data):
                kubernetes.restart_daemon_set(namespace, KUBERNETES_SCRAPER_NAME)
            return

        self._deploy_scraper(
            kubernetes,
            endpoint,
            controller,
            [model],
            handle,
        )

    def _deploy_scraper(
        self,
        kubernetes: KubernetesScraperClient,
        endpoint: str,
        controller: str,
        scraped: list[str],
        handle: LogScraperHandle,
    ) -> None:
        self._register_scraper(kubernetes, controller, handle)
        namespace = scraper_namespace(controller)
        kubernetes.ensure_namespace(namespace)
        existing_data = kubernetes.read_config_map_data(namespace, KUBERNETES_SCRAPER_NAME)
        if existing_data is not None:
            existing_namespaces = [
                name for name in existing_data.get(KUBERNETES_NAMESPACES_KEY, "").split("\n") if name
            ]
            scraped = list(dict.fromkeys([*existing_namespaces, *scraped]))
        changed = self._reconcile_config_map(kubernetes, namespace, endpoint, controller, scraped, existing_data)
        kubernetes.apply_daemon_set(namespace, kubernetes_daemon_set(namespace, controller))
        if changed:
            kubernetes.restart_daemon_set(namespace, KUBERNETES_SCRAPER_NAME)

    def _register_scraper(self, kubernetes: KubernetesScraperClient, controller: str, handle: LogScraperHandle) -> None:
        if self.resource_registry.is_registered(handle):
            return
        namespace = scraper_namespace(controller)
        self.resource_registry.register(
            handle=handle,
            destroyer=lambda: kubernetes.delete_namespace(namespace),
            parent=JujuControllerHandle(controller),
        )
