from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Iterable

from juju.backend import JujuBackend, JujuExecOutput, JujuTask
from juju.models import JujuApplicationInfo, JujuIntegration, JujuIntegrationApplication

from validators.base.validator import ValidationResult


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

    def deploy_bundle_file(self, model: str, bundle: str) -> None:
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
    ) -> None:
        raise NotImplementedError

    def configure_application(self, model: str, application: str, values: dict[str, str]) -> None:
        raise NotImplementedError

    def get_application_config(self, model: str, application: str) -> dict[str, Any]:
        raise NotImplementedError

    def bootstrap_controller(self, cloud: str, controller: str) -> None:
        raise NotImplementedError

    def add_model(self, controller: str, model: str, model_config: dict[str, str]) -> None:
        raise NotImplementedError

    def switch(self, controller: str, model: str) -> None:
        raise NotImplementedError

    def scp(self, model: str, source: str, destination: str) -> None:
        raise NotImplementedError

    def ssh(self, model: str, application: str, command: str) -> None:
        raise NotImplementedError

    def unit_ip(self, model: str, unit: str) -> str:
        raise NotImplementedError

    def version(self, model: str) -> str:
        raise NotImplementedError

    def validate_application(self, model: str, application: str, level: str) -> dict[str, list[ValidationResult]]:
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

    # Implementation of methods mocking a JujuBackend

    def list_applications(self, model: str) -> dict[str, JujuApplicationInfo]:
        """Return list of application names in the model"""
        return {key: JujuApplicationInfo(charm=value, revision=0) for key, value in self.applications.items()}

    def application_charm(self, model: str, application: str) -> str:
        """Return the charm name for a given application"""
        return self.applications[application]

    def integrate(self, model: str, target_1: JujuIntegrationApplication, target_2: JujuIntegrationApplication) -> None:
        """Mock integrating two applications (captures call for verification)"""
        self.integrations.append(
            (model, target_1.application, target_1.endpoint, target_2.application, target_2.endpoint)
        )

    def integration_exists(
        self, application1: str, endpoint1: str, application2: str, endpoint2: str, model: str
    ) -> bool:
        """Check if an integration exists between two applications

        We treat integrations as undirected for simplicity.

        """
        for m, app1, endp1, app2, endp2 in self.integrations:
            if m != model:
                continue
            if application1 == app1 and endpoint1 == endp1 and application2 == app2 and endpoint2 == endp2:
                return True
            if application1 == app2 and endpoint1 == endp2 and application2 == app1 and endpoint2 == endp1:
                return True
        return False

    def deploy_application(
        self,
        model: str,
        charm: str,
        application: str | None = None,
        config: dict[str, Any] | None = None,
        trust: bool = False,
    ) -> None:
        """Mock deploying an application (captures call for verification)"""
        self.deployed.append((model, charm, application))  # Ignoring config and trust for simplicity

    def configure_application(self, model: str, application: str, values: dict[str, Any]) -> None:
        """Mock configuring an application (captures call for verification)"""
        self.configured_applications.append((model, application, values))

    def get_application_config(self, model: str, application: str) -> dict[str, Any]:
        """Mock getting application configuration (returns empty dict for unset apps)"""
        for m, app, config in self.configured_applications:
            if m == model and app == application:
                return config
        return {}

    def wait_idle(
        self,
        model: str,
        timeout: timedelta | None,
        count: int | None,
        strict_timeout: bool = False,
        applications: Iterable[str] | None = None,
    ) -> None:
        """Wait for model to become idle (captures call for verification)"""
        self.waited_idle.append((model, str(timeout), count, strict_timeout, applications))

    def wait_application_scaled(self, model: str, application: str, timeout: timedelta | None) -> None:
        """Wait for application to be scaled (captures call for verification)"""
        self.waited_scaled.append((model, application, str(timeout)))

    def wait_application_settled(self, model: str, application: str, timeout: timedelta | None) -> None:
        """Wait for application to settle (captures call for verification)"""
        self.waited_settled.append((model, application, str(timeout)))

    def wait_for_unit_message(self, model: str, unit: str, message: str, timeout: timedelta | None) -> None:
        """Wait for a specific message from a unit (captures call for verification)"""
        self.waited_messages.append((model, unit, message, str(timeout)))

    def scp(self, model: str, source: str, destination: str) -> None:
        """Mock SCP file transfer (captures call for verification)"""
        self.scp_calls.append((model, source, destination))

    def ssh(self, model: str, target: str, command: str) -> None:
        """Mock SSH command execution (captures call for verification)"""
        self.ssh_calls.append((model, target, command))

    def run_action(self, model: str, unit: str, action: str, params: dict[str, Any]) -> JujuTask:
        """Mock running an action on a unit (captures call for verification)"""
        self.actions.append((model, unit, action, params))
        return JujuTask("", 0, "", "", "")  # Return dummy value; we're not really using this (yet).

    def unit_ip(self, model: str, unit: str) -> str:
        """Return the IP address of a unit"""
        return self.unit_ips[unit]

    def remove_applications(self, model: str, *applications: str) -> None:
        """Mock removing applications (captures call for verification)"""
        for application in applications:
            self.removed.append((model, application))

    def wait_for_removal(self, model: str, applications: list[str], timeout: timedelta | None) -> None:
        """Mock waiting for application removal (captures call for verification)"""
        self.waited_removal.append((model, applications, str(timeout)))

    def num_units(self, model: str, application: str) -> int:
        return 0
