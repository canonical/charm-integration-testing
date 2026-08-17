from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

from juju.backend import JujuBackend, JujuExecOutput, JujuTask
from juju.models import JujuApplicationInfo, JujuConsumedOfferInfo, JujuIntegration, JujuIntegrationApplication
from juju.resource_registry.handles import JujuModelHandle
from juju.version import JujuVersion
from kubernetes_client import KubernetesClient

from validators.base.validator import ValidationResult


def _model_key(model: JujuModelHandle | str) -> str:
    """Normalize a model reference for recording/comparison in test stubs.

    Accepts either the new JujuModelHandle or a plain str for legacy callers
    that still pass a model name/URI.
    """
    return model.uri if isinstance(model, JujuModelHandle) else model


class NullJujuBackend(JujuBackend):
    """Concrete JujuBackend where every method raises NotImplementedError.

    Extend this in tests and override only the methods you need.
    """

    def scale_application(self, model: str, application: str, num: int) -> None:
        raise NotImplementedError

    def num_units(self, model: str, application: str) -> int:
        raise NotImplementedError

    def list_applications(self, model: str) -> dict[str, JujuApplicationInfo]:
        raise NotImplementedError

    def list_consumed_offers(self, model: str) -> dict[str, JujuConsumedOfferInfo]:
        raise NotImplementedError

    def list_offers(self, model: str) -> set[str]:
        raise NotImplementedError

    def create_offer(self, model: str, app: str, endpoints: list[str], offer_name: str) -> None:
        raise NotImplementedError

    def list_integrations(self, model: str) -> set[JujuIntegration]:
        raise NotImplementedError

    def integration_exists(
        self, application_1: str, endpoint_1: str, application_2: str, endpoint_2: str, model: str
    ) -> bool:
        raise NotImplementedError

    def wait_idle(
        self,
        model: str,
        timeout: timedelta | None,
        count: int | None,
        strict_timeout: bool = False,
        applications: list[str] | None = None,
    ) -> None:
        raise NotImplementedError

    def wait_idle_multi_model(
        self,
        models: list[str],
        timeout: timedelta | None,
        count: int | None,
        strict_timeout: bool = False,
    ) -> None:
        raise NotImplementedError

    def wait_application_settled(self, model: str, application: str, timeout: timedelta | None) -> None:
        raise NotImplementedError

    def wait_application_scaled(self, model: str, application: str, timeout: timedelta | None) -> None:
        raise NotImplementedError

    def wait_for_unit_message(self, model: str, unit: str, message: str, timeout: timedelta | None) -> None:
        raise NotImplementedError

    def juju_status_text(self, model: str) -> str:
        raise NotImplementedError

    def integrate(self, model: str, target_1: JujuIntegrationApplication, target_2: JujuIntegrationApplication) -> None:
        raise NotImplementedError

    def remove_integration(
        self, model: str, target_1: JujuIntegrationApplication, target_2: JujuIntegrationApplication
    ) -> None:
        raise NotImplementedError

    def deploy_bundle_file(
        self, model: str, bundle: str, timeout: timedelta | None = None, trust: bool = False, force: bool = False
    ) -> None:
        raise NotImplementedError

    def refresh_application(
        self,
        model: str,
        application: str,
        revision: int | None = None,
        channel: str | None = None,
    ) -> None:
        raise NotImplementedError

    def remove_applications(self, model: str, *applications: str) -> None:
        raise NotImplementedError

    def wait_for_removal(self, model: str, applications: list[str], timeout: timedelta | None) -> None:
        raise NotImplementedError

    def wait_for_removal_of_integration(
        self,
        model: str,
        endpoint_1: JujuIntegrationApplication,
        endpoint_2: JujuIntegrationApplication,
        timeout: timedelta | None,
    ) -> None:
        raise NotImplementedError

    def wait_for_removal_of_units(self, model: str, applications: list[str], timeout: timedelta | None) -> None:
        raise NotImplementedError

    def application_charm(self, model: str, application: str) -> str | None:
        raise NotImplementedError

    def application_units(self, model: str, application: str) -> list[str]:
        raise NotImplementedError

    def exec_unit(self, model: str, unit: str, task: str, operator: bool = False) -> JujuExecOutput:
        raise NotImplementedError

    def run_action(self, model: str, unit: str, action: str, arguments: dict[str, Any]) -> JujuTask:
        raise NotImplementedError

    def add_secret(self, model: str, name: str, values: dict[str, str]) -> str:
        raise NotImplementedError

    def read_secret(self, model: str, name_or_id: str) -> dict[str, str]:
        raise NotImplementedError

    def grant_secret(self, model: str, name_or_id: str, application: str) -> None:
        raise NotImplementedError

    def remove_secret(self, model: str, name_or_id: str) -> None:
        raise NotImplementedError

    def deploy_application(
        self,
        model: str,
        charm: str,
        application: str | None = None,
        config: dict[str, Any] | None = None,
        trust: bool = False,
        force: bool = False,
    ) -> None:
        raise NotImplementedError

    def configure_application(self, model: str, application: str, values: dict[str, str]) -> None:
        raise NotImplementedError

    def get_application_config(self, model: str, application: str) -> dict[str, Any]:
        raise NotImplementedError

    def bootstrap_controller(
        self,
        cloud: str,
        controller: str,
        controller_constraints: dict[str, str],
        bootstrap_configuration: dict[str, str],
        metadata_source: Any | None = None,
        agent_version: str | None = None,
    ) -> None:
        raise NotImplementedError

    def add_model(self, controller: str, model: str, model_config: dict[str, str]) -> None:
        raise NotImplementedError

    def scp(self, model: str, source: str, destination: str) -> None:
        raise NotImplementedError

    def ssh(self, model: str, application: str, command: str) -> None:
        raise NotImplementedError

    def unit_ip(self, model: str, unit: str) -> str:
        raise NotImplementedError

    def reboot_model_controller(self, model: str) -> None:
        raise NotImplementedError

    def is_k8s_model(self, model: str) -> bool:
        raise NotImplementedError

    def reboot_model_controller_leader(self, model: str) -> None:
        raise NotImplementedError

    def version(self, model: str) -> JujuVersion:
        raise NotImplementedError

    def cli_version(self) -> JujuVersion:
        raise NotImplementedError

    def validate_application(self, model: str, application: str, level: str) -> dict[str, list[ValidationResult]]:
        raise NotImplementedError

    def get_kubernetes_client(self, cloud: str) -> KubernetesClient:
        raise NotImplementedError

    def kill_controller(self, controller: str) -> None:
        raise NotImplementedError

    def wait_for_model_to_exist(self, model: str, timeout: timedelta | None) -> None:
        raise NotImplementedError

    def migrate_model(self, model_name: str, source_controller: str, target_controller: str) -> None:
        raise NotImplementedError

    def upgrade_controller(self, controller: str, agent_version: str | None = None) -> None:
        raise NotImplementedError

    def upgrade_model(self, model: str, agent_version: str | None = None) -> None:
        raise NotImplementedError

    def wait_for_application_revision(
        self,
        application: str,
        expected_revision: int,
        timeout: timedelta | None,
        model: str = "default",
    ) -> None:
        raise NotImplementedError

    def debug_log(self, model: str) -> str:
        raise NotImplementedError

    def get_controller_kubeconfig(self, controller: str) -> Path | None:
        raise NotImplementedError


@dataclass
class JujuStub(NullJujuBackend):
    deployed: list[Any] = field(default_factory=list)
    configured: list[Any] = field(default_factory=list)
    waited_idle: list[Any] = field(default_factory=list)
    waited_messages: list[Any] = field(default_factory=list)
    waited_scaled: list[Any] = field(default_factory=list)
    waited_settled: list[Any] = field(default_factory=list)
    integrations: list[Any] = field(default_factory=list)
    scp_calls: list[Any] = field(default_factory=list)
    ssh_calls: list[Any] = field(default_factory=list)
    actions: list[Any] = field(default_factory=list)
    applications: dict[str, str] = field(default_factory=dict)
    configured_applications: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    unit_ips: dict[str, str] = field(default_factory=dict)
    removed: list[Any] = field(default_factory=list)
    waited_removal: list[Any] = field(default_factory=list)
    is_k8s: bool = True

    # Implementation of methods mocking a JujuBackend

    def list_applications(self, model: "JujuModelHandle | str") -> dict[str, JujuApplicationInfo]:
        """Return list of application names in the model"""
        return {key: JujuApplicationInfo(charm=value, revision=0) for key, value in self.applications.items()}

    def application_charm(self, model: "JujuModelHandle | str", application: str) -> str:
        """Return the charm name for a given application"""
        return self.applications[application]

    def integrate(
        self, model: "JujuModelHandle | str", target_1: JujuIntegrationApplication, target_2: JujuIntegrationApplication
    ) -> None:
        """Mock integrating two applications (captures call for verification)"""
        self.integrations.append(
            (_model_key(model), target_1.application, target_1.endpoint, target_2.application, target_2.endpoint)
        )

    def integration_exists(
        self, application1: str, endpoint1: str, application2: str, endpoint2: str, model: "JujuModelHandle | str"
    ) -> bool:
        """Check if an integration exists between two applications

        We treat integrations as undirected for simplicity.

        """
        for m, app1, endp1, app2, endp2 in self.integrations:
            if m != _model_key(model):
                continue
            if application1 == app1 and endpoint1 == endp1 and application2 == app2 and endpoint2 == endp2:
                return True
            if application1 == app2 and endpoint1 == endp2 and application2 == app1 and endpoint2 == endp1:
                return True
        return False

    def deploy_application(
        self,
        model: "JujuModelHandle | str",
        charm: str,
        application: str | None = None,
        config: dict[str, Any] | None = None,
        trust: bool = False,
        force: bool = False,
    ) -> None:
        """Mock deploying an application (captures call for verification)"""
        self.deployed.append((_model_key(model), charm, application))  # Ignoring config, trust, force for simplicity

    def configure_application(self, model: "JujuModelHandle | str", application: str, values: dict[str, Any]) -> None:
        """Mock configuring an application (captures call for verification)"""
        self.configured_applications.append((_model_key(model), application, values))

    def get_application_config(self, model: "JujuModelHandle | str", application: str) -> dict[str, Any]:
        """Mock getting application configuration (returns empty dict for unset apps)"""
        for m, app, config in self.configured_applications:
            if m == _model_key(model) and app == application:
                return config
        return {}

    def wait_idle(
        self,
        model: "JujuModelHandle | str",
        timeout: timedelta | None,
        count: int | None,
        strict_timeout: bool = False,
        applications: Iterable[str] | None = None,
    ) -> None:
        """Wait for model to become idle (captures call for verification)"""
        self.waited_idle.append((_model_key(model), str(timeout), count, strict_timeout, applications))

    def wait_application_scaled(
        self, model: "JujuModelHandle | str", application: str, timeout: timedelta | None
    ) -> None:
        """Wait for application to be scaled (captures call for verification)"""
        self.waited_scaled.append((_model_key(model), application, str(timeout)))

    def wait_application_settled(
        self, model: "JujuModelHandle | str", application: str, timeout: timedelta | None
    ) -> None:
        """Wait for application to settle (captures call for verification)"""
        self.waited_settled.append((_model_key(model), application, str(timeout)))

    def wait_for_unit_message(
        self, model: "JujuModelHandle | str", unit: str, message: str, timeout: timedelta | None
    ) -> None:
        """Wait for a specific message from a unit (captures call for verification)"""
        self.waited_messages.append((_model_key(model), unit, message, str(timeout)))

    def scp(self, model: "JujuModelHandle | str", source: str, destination: str) -> None:
        """Mock SCP file transfer (captures call for verification)"""
        self.scp_calls.append((_model_key(model), source, destination))

    def ssh(self, model: "JujuModelHandle | str", target: str, command: str) -> None:
        """Mock SSH command execution (captures call for verification)"""
        self.ssh_calls.append((_model_key(model), target, command))

    def run_action(self, model: "JujuModelHandle | str", unit: str, action: str, params: dict[str, Any]) -> JujuTask:
        """Mock running an action on a unit (captures call for verification)"""
        self.actions.append((_model_key(model), unit, action, params))
        return JujuTask("", 0, "", "", "")  # Return dummy value; we're not really using this (yet).

    def unit_ip(self, model: "JujuModelHandle | str", unit: str) -> str:
        """Return the IP address of a unit"""
        return self.unit_ips[unit]

    def is_k8s_model(self, model: "JujuModelHandle | str") -> bool:
        """Return whether the model is a k8s model"""
        return self.is_k8s

    def remove_applications(self, model: "JujuModelHandle | str", *applications: str) -> None:
        """Mock removing applications (captures call for verification)"""
        for application in applications:
            self.removed.append((_model_key(model), application))

    def wait_for_removal(
        self, model: "JujuModelHandle | str", applications: list[str], timeout: timedelta | None
    ) -> None:
        """Mock waiting for application removal (captures call for verification)"""
        self.waited_removal.append((_model_key(model), applications, str(timeout)))

    def num_units(self, model: "JujuModelHandle | str", application: str) -> int:
        return 0
