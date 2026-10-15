# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest
import yaml
from extensions.remote_logging.extension import (
    LogScraperHandle,
    RemoteLoggingExtension,
    controller_namespace,
    scraper_namespace,
)
from extensions.remote_logging.scraper import KUBERNETES_NAMESPACES_KEY, KUBERNETES_SCRAPER_NAME, USERDATA_KEY
from juju.handles import JujuControllerHandle, JujuModelHandle
from kubernetes_client import KubernetesClient
from resource_registry import ResourceRegistry

from ..shared import NullJujuBackend

CONTROLLER = "test-controller"
LOKI_URL = "http://observability.example/loki-model-loki"
EXPORTER_ENDPOINT = f"{LOKI_URL}/loki/api/v1/push"


@pytest.mark.parametrize("controller", ["CTRL_1", "a" * 100])
def test_scraper_namespace_is_dns_compatible_and_bounded(controller: str) -> None:
    namespace = scraper_namespace(controller)

    assert len(namespace) <= 63
    assert namespace == namespace.lower()
    assert namespace[0] != "-" and namespace[-1] != "-"
    assert all(character.isalnum() or character == "-" for character in namespace)


@dataclass
class KubernetesStub:
    config_maps: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    daemon_sets: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    namespaces: list[str] = field(default_factory=list)
    deleted_namespaces: list[str] = field(default_factory=list)
    restarts: list[tuple[str, str]] = field(default_factory=list)

    def ensure_namespace(self, name: str) -> None:
        self.namespaces.append(name)

    def delete_namespace(self, name: str) -> None:
        self.deleted_namespaces.append(name)

    def apply_config_map(self, namespace: str, body: dict[str, Any]) -> None:
        self.config_maps[(namespace, body["metadata"]["name"])] = body

    def read_config_map_data(self, namespace: str, name: str) -> dict[str, str] | None:
        body = self.config_maps.get((namespace, name))
        return None if body is None else dict(body["data"])

    def apply_daemon_set(self, namespace: str, body: dict[str, Any]) -> None:
        self.daemon_sets.append((namespace, body))

    def restart_daemon_set(self, namespace: str, name: str) -> None:
        self.restarts.append((namespace, name))


@dataclass
class JujuStub(NullJujuBackend):
    k8s_clouds: set[str] = field(default_factory=set)
    model_clouds: dict[str, str] = field(default_factory=dict)
    kubernetes: KubernetesStub = field(default_factory=KubernetesStub)
    kubernetes_by_cloud: dict[str, KubernetesStub] = field(default_factory=dict)
    controller_cloud_queries: list[tuple[str, str]] = field(default_factory=list)

    def is_k8s_cloud(self, cloud: str) -> bool:
        return cloud in self.k8s_clouds

    def is_k8s_cloud_on_controller(self, cloud: str, controller: str) -> bool:
        self.controller_cloud_queries.append((cloud, controller))
        return cloud in self.k8s_clouds

    def get_kubernetes_client_for_controller(self, controller: str) -> KubernetesClient | None:
        return self.kubernetes if controller in self.k8s_clouds else None  # type: ignore[return-value]

    def get_kubernetes_client_for_model(self, model: JujuModelHandle) -> KubernetesClient | None:
        cloud = self.model_clouds.get(model.model)
        if cloud not in self.k8s_clouds:
            return None
        return self.kubernetes_by_cloud.get(cloud, self.kubernetes)  # type: ignore[return-value]


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test-remote-logging")


def _registry_with_controller(logger: logging.Logger) -> ResourceRegistry:
    registry = ResourceRegistry([], logger)
    registry.register(JujuControllerHandle(CONTROLLER))
    return registry


def _extension(
    juju: JujuStub,
    logger: logging.Logger,
    endpoints: dict[str, str] | None = None,
    registry: ResourceRegistry | None = None,
) -> RemoteLoggingExtension:
    return RemoteLoggingExtension(
        juju,
        logger,
        {CONTROLLER: LOKI_URL} if endpoints is None else endpoints,
        registry or _registry_with_controller(logger),
    )


class TestMachineControllers:
    def test_bootstrap_adds_userdata_for_the_controller_machine(self, logger: logging.Logger) -> None:
        # GIVEN a machine cloud and a configured endpoint
        extension = _extension(JujuStub(), logger)

        # WHEN the bootstrap configuration is prepared
        config = extension.pre_bootstrap_controller("lxd", CONTROLLER, {"foo": "bar"})

        # THEN the userdata installs a collector exporting to the endpoint, labelled with the controller
        assert config["foo"] == "bar"
        userdata = yaml.safe_load(config[USERDATA_KEY])
        collector_config = yaml.safe_load(userdata["write_files"][0]["content"])
        assert collector_config["exporters"]["loki"]["endpoint"] == EXPORTER_ENDPOINT
        assert collector_config["receivers"]["filelog"]["include"] == ["/var/log/juju/*.log"]
        assert collector_config["receivers"]["filelog"]["resource"] == {
            "juju_controller": CONTROLLER,
            "juju_model": "controller",
        }
        assert any("systemctl enable --now otelcol-scraper" in command for command in userdata["postruncmd"])

    def test_bootstrap_leaves_kubernetes_clouds_unchanged(self, logger: logging.Logger) -> None:
        extension = _extension(JujuStub(k8s_clouds={"k8s"}), logger)

        assert extension.pre_bootstrap_controller("k8s", CONTROLLER, {"foo": "bar"}) == {"foo": "bar"}

    def test_bootstrap_leaves_unconfigured_controllers_unchanged(self, logger: logging.Logger) -> None:
        extension = _extension(JujuStub(), logger, endpoints={})

        assert extension.pre_bootstrap_controller("lxd", CONTROLLER, {}) == {}

    def test_add_model_adds_userdata_labelled_with_the_model(self, logger: logging.Logger) -> None:
        extension = _extension(JujuStub(), logger)

        config = extension.pre_add_model(CONTROLLER, "my-model", {"foo": "bar"}, "lxd")

        collector_config = yaml.safe_load(yaml.safe_load(config[USERDATA_KEY])["write_files"][0]["content"])
        assert config["foo"] == "bar"
        assert collector_config["receivers"]["filelog"]["resource"]["juju_model"] == "my-model"

    def test_add_model_keeps_userdata_provided_by_the_user(self, logger: logging.Logger) -> None:
        # GIVEN user provided cloud-init data
        existing = yaml.safe_dump({"packages": ["jq"], "postruncmd": ["echo user"]})
        extension = _extension(JujuStub(), logger)

        # WHEN the model config is extended
        config = extension.pre_add_model(CONTROLLER, "my-model", {USERDATA_KEY: existing}, "lxd")

        # THEN the user's entries are preserved ahead of ours
        userdata = yaml.safe_load(config[USERDATA_KEY])
        assert userdata["packages"] == ["jq"]
        assert userdata["postruncmd"][0] == "echo user"
        assert len(userdata["postruncmd"]) > 1

    def test_add_model_leaves_unconfigured_controllers_unchanged(self, logger: logging.Logger) -> None:
        extension = _extension(JujuStub(), logger, endpoints={})

        assert extension.pre_add_model(CONTROLLER, "my-model", {"foo": "bar"}, "lxd") == {"foo": "bar"}


class TestKubernetesControllers:
    def test_bootstrap_deploys_the_scraper_for_the_controller_namespace(self, logger: logging.Logger) -> None:
        # GIVEN a Kubernetes controller
        juju = JujuStub(k8s_clouds={CONTROLLER})
        extension = _extension(juju, logger)

        # WHEN the controller is bootstrapped
        extension.post_bootstrap_controller(CONTROLLER)

        # THEN a scraper exports the controller namespace's pod logs to the endpoint
        namespace = scraper_namespace(CONTROLLER)
        assert juju.kubernetes.namespaces == [namespace]
        data = juju.kubernetes.config_maps[(namespace, KUBERNETES_SCRAPER_NAME)]["data"]
        assert data[KUBERNETES_NAMESPACES_KEY] == controller_namespace(CONTROLLER)
        collector_config = yaml.safe_load(data["config.yaml"])
        assert collector_config["exporters"]["loki"]["endpoint"] == EXPORTER_ENDPOINT
        assert collector_config["receivers"]["filelog"]["include"] == [
            f"/var/log/pods/{controller_namespace(CONTROLLER)}_*/*/*.log"
        ]
        assert [(ns, body["kind"]) for ns, body in juju.kubernetes.daemon_sets] == [(namespace, "DaemonSet")]

    def test_bootstrap_skips_controller_without_endpoint(self, logger: logging.Logger) -> None:
        juju = JujuStub(k8s_clouds={CONTROLLER})

        _extension(juju, logger, endpoints={}).post_bootstrap_controller(CONTROLLER)

        assert juju.kubernetes.daemon_sets == []

    def test_add_model_extends_the_scraper_and_restarts_it(self, logger: logging.Logger) -> None:
        # GIVEN a deployed scraper
        juju = JujuStub(k8s_clouds={CONTROLLER}, model_clouds={"my-model": CONTROLLER})
        extension = _extension(juju, logger)
        extension.post_bootstrap_controller(CONTROLLER)

        # WHEN a model is added
        config = extension.pre_add_model(CONTROLLER, "my-model", {"foo": "bar"}, CONTROLLER)
        extension.post_add_model_with_cloud(CONTROLLER, "my-model", CONTROLLER)

        # THEN the model config is untouched and its namespace is scraped
        namespace = scraper_namespace(CONTROLLER)
        assert config == {"foo": "bar"}
        data = juju.kubernetes.config_maps[(namespace, KUBERNETES_SCRAPER_NAME)]["data"]
        assert data[KUBERNETES_NAMESPACES_KEY].split("\n") == [controller_namespace(CONTROLLER), "my-model"]
        assert yaml.safe_load(data["config.yaml"])["receivers"]["filelog"]["include"] == [
            f"/var/log/pods/{controller_namespace(CONTROLLER)}_*/*/*.log",
            "/var/log/pods/my-model_*/*/*.log",
        ]
        assert juju.kubernetes.restarts == [(namespace, KUBERNETES_SCRAPER_NAME)]

    def test_add_model_already_scraped_does_not_restart(self, logger: logging.Logger) -> None:
        juju = JujuStub(k8s_clouds={CONTROLLER}, model_clouds={"my-model": CONTROLLER})
        extension = _extension(juju, logger)
        extension.post_bootstrap_controller(CONTROLLER)
        extension.post_add_model_with_cloud(CONTROLLER, "my-model", CONTROLLER)

        # AND a fresh extension instance, as created for every test
        _extension(juju, logger).post_add_model_with_cloud(CONTROLLER, "my-model", CONTROLLER)

        assert len(juju.kubernetes.restarts) == 1

    def test_existing_scraper_reconciles_changed_endpoint(self, logger: logging.Logger) -> None:
        juju = JujuStub(k8s_clouds={CONTROLLER}, model_clouds={"my-model": CONTROLLER})
        _extension(juju, logger).post_bootstrap_controller(CONTROLLER)
        _extension(juju, logger).post_add_model_with_cloud(CONTROLLER, "my-model", CONTROLLER)
        updated = _extension(juju, logger, endpoints={CONTROLLER: "https://new-observability.example/loki"})

        updated.post_add_model_with_cloud(CONTROLLER, "my-model", CONTROLLER)

        namespace = scraper_namespace(CONTROLLER)
        data = juju.kubernetes.config_maps[(namespace, KUBERNETES_SCRAPER_NAME)]["data"]
        assert yaml.safe_load(data["config.yaml"])["exporters"]["loki"]["endpoint"] == (
            "https://new-observability.example/loki/api/v1/push"
        )
        assert juju.kubernetes.restarts[-1] == (namespace, KUBERNETES_SCRAPER_NAME)

    def test_bootstrap_reconciles_existing_scraper_configuration(self, logger: logging.Logger) -> None:
        juju = JujuStub(k8s_clouds={CONTROLLER})
        _extension(juju, logger).post_bootstrap_controller(CONTROLLER)
        updated = _extension(juju, logger, endpoints={CONTROLLER: "https://new-observability.example/loki"})

        updated.post_bootstrap_controller(CONTROLLER)

        namespace = scraper_namespace(CONTROLLER)
        data = juju.kubernetes.config_maps[(namespace, KUBERNETES_SCRAPER_NAME)]["data"]
        assert yaml.safe_load(data["config.yaml"])["exporters"]["loki"]["endpoint"] == (
            "https://new-observability.example/loki/api/v1/push"
        )
        assert juju.kubernetes.restarts == [(namespace, KUBERNETES_SCRAPER_NAME)]

    def test_kubernetes_model_on_machine_controller_deploys_scraper(self, logger: logging.Logger) -> None:
        # GIVEN a machine controller managing a model on a Kubernetes cloud
        juju = JujuStub(k8s_clouds={"workload-k8s"}, model_clouds={"my-model": "workload-k8s"})
        extension = _extension(juju, logger)

        # WHEN the model is configured and added
        config = extension.pre_add_model(CONTROLLER, "my-model", {"foo": "bar"}, "workload-k8s")
        extension.post_add_model_with_cloud(CONTROLLER, "my-model", "workload-k8s")

        # THEN machine userdata is not added and a scraper is deployed in the model's cluster
        assert config == {"foo": "bar"}
        assert juju.controller_cloud_queries == [("workload-k8s", CONTROLLER)]
        namespace = scraper_namespace(CONTROLLER)
        data = juju.kubernetes.config_maps[(namespace, KUBERNETES_SCRAPER_NAME)]["data"]
        assert data[KUBERNETES_NAMESPACES_KEY] == "my-model"
        assert LogScraperHandle(CONTROLLER, "workload-k8s") in extension.resource_registry.registered_handles()

    def test_machine_model_on_kubernetes_controller_gets_userdata(self, logger: logging.Logger) -> None:
        # GIVEN a Kubernetes controller managing a model on a machine cloud
        juju = JujuStub(k8s_clouds={CONTROLLER})
        extension = _extension(juju, logger)

        # WHEN the model configuration is prepared
        config = extension.pre_add_model(CONTROLLER, "my-model", {}, "lxd")
        extension.post_add_model_with_cloud(CONTROLLER, "my-model", "lxd")

        # THEN the model gets machine logging and no Kubernetes scraper is deployed for it
        assert USERDATA_KEY in config
        assert juju.kubernetes.config_maps == {}

    def test_models_on_different_kubernetes_clouds_get_independent_scrapers(self, logger: logging.Logger) -> None:
        # GIVEN one controller managing models on two Kubernetes clouds
        first = KubernetesStub()
        second = KubernetesStub()
        juju = JujuStub(
            k8s_clouds={"first-k8s", "second-k8s"},
            model_clouds={"model-a": "first-k8s", "model-b": "second-k8s"},
            kubernetes_by_cloud={"first-k8s": first, "second-k8s": second},
        )
        extension = _extension(juju, logger)

        # WHEN both models are added
        extension.post_add_model_with_cloud(CONTROLLER, "model-a", "first-k8s")
        extension.post_add_model_with_cloud(CONTROLLER, "model-b", "second-k8s")

        # THEN each workload cluster has its own scraper and teardown handle
        namespace = scraper_namespace(CONTROLLER)
        assert first.config_maps[(namespace, KUBERNETES_SCRAPER_NAME)]["data"][KUBERNETES_NAMESPACES_KEY] == "model-a"
        assert second.config_maps[(namespace, KUBERNETES_SCRAPER_NAME)]["data"][KUBERNETES_NAMESPACES_KEY] == "model-b"
        handles = extension.resource_registry.registered_handles()
        assert LogScraperHandle(CONTROLLER, "first-k8s") in handles
        assert LogScraperHandle(CONTROLLER, "second-k8s") in handles

    def test_registry_teardown_removes_the_scraper_namespace(self, logger: logging.Logger) -> None:
        # GIVEN a deployed scraper and a registered controller
        juju = JujuStub(k8s_clouds={CONTROLLER})
        registry = _registry_with_controller(logger)
        controller_handle = JujuControllerHandle(CONTROLLER)
        _extension(juju, logger, registry=registry).post_bootstrap_controller(CONTROLLER)
        assert juju.kubernetes.deleted_namespaces == []

        # WHEN the controller is torn down
        registry.teardown(controller_handle)

        # THEN the scraper namespace is deleted
        assert juju.kubernetes.deleted_namespaces == [scraper_namespace(CONTROLLER)]

    def test_missing_namespace_data_is_rebuilt(self, logger: logging.Logger) -> None:
        # GIVEN a stale scraper ConfigMap without the namespace bookkeeping key
        juju = JujuStub(k8s_clouds={CONTROLLER}, model_clouds={"my-model": CONTROLLER})
        namespace = scraper_namespace(CONTROLLER)
        juju.kubernetes.config_maps[(namespace, KUBERNETES_SCRAPER_NAME)] = {
            "metadata": {"name": KUBERNETES_SCRAPER_NAME},
            "data": {"config.yaml": "stale"},
        }

        # WHEN the model is added
        _extension(juju, logger).post_add_model_with_cloud(CONTROLLER, "my-model", CONTROLLER)

        # THEN the generated ConfigMap is repaired and the scraper is restarted
        data = juju.kubernetes.config_maps[(namespace, KUBERNETES_SCRAPER_NAME)]["data"]
        assert data[KUBERNETES_NAMESPACES_KEY].split("\n") == [controller_namespace(CONTROLLER), "my-model"]
        assert [(deployed_namespace, body["kind"]) for deployed_namespace, body in juju.kubernetes.daemon_sets] == [
            (namespace, "DaemonSet")
        ]
        assert juju.kubernetes.restarts == [(namespace, KUBERNETES_SCRAPER_NAME)]

    def test_machine_controller_registers_nothing(self, logger: logging.Logger) -> None:
        registry = ResourceRegistry([], logger)

        _extension(JujuStub(), logger, registry=registry).post_bootstrap_controller(CONTROLLER)

        assert registry.registered_handles() == []


def test_log_scraper_handle_sanitizes_unsafe_path_characters() -> None:
    handle = LogScraperHandle("ctrl.with spaces/and:colons", "cloud/with spaces")

    assert handle.path_segment == "remote-logging-scraper-ctrl-with-spaces-and-colons-cloud-with-spaces"
