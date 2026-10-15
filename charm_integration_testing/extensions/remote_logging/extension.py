# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
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
        logger: logging.Logger,
        endpoints_by_controller: dict[str, str],
        resource_registry: ResourceRegistry,
    ) -> None:
        self.juju = juju
        self.logger = logger
        self.endpoints_by_controller = endpoints_by_controller
        self.resource_registry = resource_registry

    def pre_bootstrap_controller(
        self, cloud: str, controller: str, bootstrap_configuration: dict[str, str]
    ) -> dict[str, str]:
        endpoint = self.endpoints_by_controller.get(controller)
        if endpoint is None or self.juju.is_k8s_cloud(cloud):
            return bootstrap_configuration
        self.logger.debug(f"Shipping logs of controller '{controller}' to '{endpoint}' from its machines.")
        return self._with_userdata(bootstrap_configuration, endpoint, controller, CONTROLLER_MODEL)

    def post_bootstrap_controller(self, controller: str) -> None:
        endpoint = self.endpoints_by_controller.get(controller)
        if endpoint is None:
            self.logger.debug(f"Skipping remote logging setup for controller '{controller}': no endpoint configured.")
            return

        kubernetes = self.juju.get_kubernetes_client_for_controller(controller)
        if kubernetes is None:
            return
        namespace = scraper_namespace(controller)
        self.logger.debug(f"Deploying log scraper to namespace '{namespace}' for controller '{controller}'.")
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
            self.juju.is_k8s_cloud(cloud)
            if cloud is not None
            else self.juju.get_kubernetes_client_for_controller(controller) is not None
        )
        if is_kubernetes:
            return model_config

        self.logger.debug(f"Shipping logs of model '{model}' to '{endpoint}' from its machines.")
        return self._with_userdata(model_config, endpoint, controller, model)

    def post_add_model(self, controller: str, model: str, cloud: str | None) -> None:
        endpoint = self.endpoints_by_controller.get(controller)
        if endpoint is None:
            return

        kubernetes = self.juju.get_kubernetes_client_for_model(JujuModelHandle(controller=controller, model=model))
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
    ) -> None:
        """Add the namespace of *model* to the scraper, restarting it so the new path is picked up."""
        namespace = scraper_namespace(controller)
        scraped = [name for name in data.get(KUBERNETES_NAMESPACES_KEY, "").split("\n") if name]
        if model in scraped:
            return
        self.logger.debug(f"Adding namespace '{model}' to the log scraper of controller '{controller}'.")
        kubernetes.apply_config_map(
            namespace, kubernetes_config_map(namespace, endpoint, controller, [*scraped, model])
        )
        kubernetes.restart_daemon_set(namespace, KUBERNETES_SCRAPER_NAME)

    def _ensure_scraper(
        self,
        kubernetes: KubernetesScraperClient,
        endpoint: str,
        controller: str,
        model: str,
        cloud: str | None,
    ) -> None:
        namespace = scraper_namespace(controller)
        data = kubernetes.read_config_map_data(namespace, KUBERNETES_SCRAPER_NAME)
        if data is not None:
            kubernetes.apply_daemon_set(namespace, kubernetes_daemon_set(namespace, controller))
            self._scrape_namespace(kubernetes, endpoint, controller, model, data)
            return

        self.logger.debug(
            f"Deploying log scraper to namespace '{namespace}' for model '{model}' on controller '{controller}'."
        )
        self._deploy_scraper(
            kubernetes,
            endpoint,
            controller,
            [model],
            LogScraperHandle(controller, cloud),
        )

    def _deploy_scraper(
        self,
        kubernetes: KubernetesScraperClient,
        endpoint: str,
        controller: str,
        scraped: list[str],
        handle: LogScraperHandle,
    ) -> None:
        namespace = scraper_namespace(controller)
        kubernetes.ensure_namespace(namespace)
        kubernetes.apply_config_map(namespace, kubernetes_config_map(namespace, endpoint, controller, scraped))
        kubernetes.apply_daemon_set(namespace, kubernetes_daemon_set(namespace, controller))
        # Torn down with the controller, before it is killed: the controller's own teardown bypasses extension hooks.
        self.resource_registry.register(
            handle=handle,
            destroyer=lambda: kubernetes.delete_namespace(namespace),
            parent=JujuControllerHandle(controller),
        )
