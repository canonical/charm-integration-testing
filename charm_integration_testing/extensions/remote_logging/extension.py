# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass

from juju import JujuBackend, JujuExtension, JujuModelHandle
from juju.handles import JujuControllerHandle
from kubernetes_client import KubernetesClient
from resource_registry import ResourceRegistry

from .scraper import (
    KUBERNETES_NAMESPACES_KEY,
    KUBERNETES_SCRAPER_NAME,
    USERDATA_KEY,
    kubernetes_config_map,
    kubernetes_daemon_set,
    machine_userdata,
    merge_userdata,
)

CONTROLLER_MODEL = "controller"


def scraper_namespace(controller: str) -> str:
    return f"log-scraper-{controller}"


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
        return f"remote-logging-scraper-{self.controller}{suffix}"


class RemoteLoggingExtension(JujuExtension):
    """Ship the logs of a controller and its models to a remote OTLP receiver.

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
        kubernetes.ensure_namespace(namespace)
        kubernetes.apply_config_map(
            namespace, kubernetes_config_map(namespace, endpoint, controller, [controller_namespace(controller)])
        )
        kubernetes.apply_daemon_set(namespace, kubernetes_daemon_set(namespace, controller))
        # Torn down with the controller, before it is killed: the controller's own teardown bypasses extension hooks.
        self.resource_registry.register(
            handle=LogScraperHandle(controller),
            destroyer=lambda: kubernetes.delete_namespace(namespace),
            parent=JujuControllerHandle(controller),
        )

    def pre_add_model(
        self, controller: str, model: str, model_config: dict[str, str], cloud: str | None = None
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

    def post_add_model_on_cloud(self, controller: str, model: str, cloud: str | None = None) -> None:
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

    def _scrape_namespace(self, kubernetes: KubernetesClient, endpoint: str, controller: str, model: str) -> None:
        """Add the namespace of *model* to the scraper, restarting it so the new path is picked up."""
        namespace = scraper_namespace(controller)
        data = kubernetes.read_config_map_data(namespace, KUBERNETES_SCRAPER_NAME)
        if data is None:
            self.logger.debug(f"Not scraping logs of model '{model}': no scraper in namespace '{namespace}'.")
            return
        scraped = [name for name in data[KUBERNETES_NAMESPACES_KEY].split("\n") if name]
        if model in scraped:
            return
        self.logger.debug(f"Adding namespace '{model}' to the log scraper of controller '{controller}'.")
        kubernetes.apply_config_map(
            namespace, kubernetes_config_map(namespace, endpoint, controller, [*scraped, model])
        )
        kubernetes.restart_daemon_set(namespace, KUBERNETES_SCRAPER_NAME)

    def _ensure_scraper(
        self,
        kubernetes: KubernetesClient,
        endpoint: str,
        controller: str,
        model: str,
        cloud: str | None,
    ) -> None:
        namespace = scraper_namespace(controller)
        if kubernetes.read_config_map_data(namespace, KUBERNETES_SCRAPER_NAME) is not None:
            self._scrape_namespace(kubernetes, endpoint, controller, model)
            return

        self.logger.debug(
            f"Deploying log scraper to namespace '{namespace}' for model '{model}' on controller '{controller}'."
        )
        kubernetes.ensure_namespace(namespace)
        kubernetes.apply_config_map(namespace, kubernetes_config_map(namespace, endpoint, controller, [model]))
        kubernetes.apply_daemon_set(namespace, kubernetes_daemon_set(namespace, controller))
        self.resource_registry.register(
            handle=LogScraperHandle(controller, cloud),
            destroyer=lambda: kubernetes.delete_namespace(namespace),
            parent=JujuControllerHandle(controller),
        )
