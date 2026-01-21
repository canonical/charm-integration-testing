from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Iterable

from juju.backend import JujuBackend, JujuIntegrationApplication, JujuTask


@dataclass
class JujuStub(JujuBackend):
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

    # Implementation of methods mocking a JujuBackend

    def list_applications(self, model: str) -> set[str]:
        """Return list of application names in the model"""
        return {key for key in self.applications.keys()}

    def application_charm(self, model: str, application: str) -> str:
        """Return the charm name for a given application"""
        return self.applications[application]

    def integrate(self, model: str, target_1: JujuIntegrationApplication, target_2: JujuIntegrationApplication) -> None:
        """Mock integrating two applications (captures call for verification)"""
        self.integrations.append(
            (model, target_1.application, target_1.endpoint, target_2.application, target_2.endpoint)
        )

    def integration_exists(self, application1: str, endpoint1: str, application2: str, endpoint2: str, model: str) -> bool:
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

    def deploy_application(self, model: str, charm: str, application: str | None = None, config: dict[str, Any] | None = None) -> None:
        """Mock deploying an application (captures call for verification)"""
        self.deployed.append((model, charm, application))  # Ignoring config for simplicity

    def configure_application(self, model: str, application: str, values: dict[str, Any]) -> None:
        """Mock configuring an application (captures call for verification)"""
        self.configured_applications.append((model, application, values))

    def get_application_config(self, model: str, application: str) -> dict[str, Any]:
        """Mock getting application configuration (returns empty dict)"""
        for model, app, config in self.configured_applications:
            if model == model and app == application:
                return config
        raise KeyError(f"Application {application} not configured in model {model}")

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
        return JujuTask()  # Return dummy value; we're not really using this (yet).

    def unit_ip(self, model: str, unit: str) -> str:
        """Return the IP address of a unit"""
        return self.unit_ips[unit]

    # Dummy methods to satisfy abstract base class requirements

    def num_units(self, model: str, application: str) -> int:
        return 0

    def scale_application(self) -> None:  # type: ignore[override]
        pass

    def list_integrations(self) -> None:  # type: ignore[override]
        pass

    def juju_status_text(self) -> None:  # type: ignore[override]
        pass

    def remove_integration(self) -> None:  # type: ignore[override]
        pass

    def deploy_bundle_file(self) -> None:  # type: ignore[override]
        pass

    def remove_applications(self) -> None:  # type: ignore[override]
        pass

    def wait_for_removal(self) -> None:  # type: ignore[override]
        pass

    def wait_for_removal_of_integration(self) -> None:  # type: ignore[override]
        pass

    def wait_for_removal_of_units(self) -> None:  # type: ignore[override]
        pass

    def application_units(self) -> None:  # type: ignore[override]
        pass

    def exec_unit(self) -> None:  # type: ignore[override]
        pass

    def add_secret(self) -> None:  # type: ignore[override]
        pass

    def read_secret(self) -> None:  # type: ignore[override]
        pass

    def grant_secret(self) -> None:  # type: ignore[override]
        pass

    def remove_secret(self) -> None:  # type: ignore[override]
        pass

    def get_charm_revisions(self) -> None:  # type: ignore[override]
        pass

    def version(self) -> None:  # type: ignore[override]
        pass

    # Additional methods can be added as needed for testing
