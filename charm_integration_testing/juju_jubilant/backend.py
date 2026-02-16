# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


import dataclasses
import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable

import jubilant
import yaml
from juju import (
    JujuApplicationInfo,
    JujuIntegration,
    JujuIntegrationApplication,
    JujuStatusPerformanceWarning,
    JujuTask,
    JujuWaitState,
    JujuWaitTimeoutError,
    warn_performance,
)
from juju_cmd import JujuCmdBackend

from .client import JubilantClient
from .wait import (
    all_statuses_are_in,
    applications_are_removed,
    applications_are_scaled,
    applications_have_no_units,
    integrations_are_removed,
    units_have_message,
)


class JubilantBackend(JujuCmdBackend):
    client: JubilantClient

    default_timeout = timedelta(minutes=5)
    default_successes = 3
    default_delay = timedelta(seconds=1)

    def __init__(self, client: JubilantClient | None = None):
        super().__init__()
        self.client = client or JubilantClient()

    @warn_performance(category=JujuStatusPerformanceWarning, threshold=timedelta(seconds=5))
    def status(self, model: str) -> jubilant.Status:
        return self.client.model(model).status()

    @warn_performance(category=JujuStatusPerformanceWarning, threshold=timedelta(seconds=5))
    def juju_status_text(self, model: str) -> str:
        return self.client.model(model).cli("status", "--integrations", "--format", "tabular")

    def wait(
        self,
        model: str,
        ready: Callable[[jubilant.Status], tuple[bool, JujuWaitState]],
        error: Callable[[jubilant.Status], tuple[bool, JujuWaitState]] | None = None,
        timeout: timedelta | None = None,
        successes: int | None = None,
        delay: timedelta | None = None,
        strict_timeout: bool = False,
    ) -> None:
        # Set default parameters
        if timeout is None:
            timeout = self.default_timeout
        if successes is None:
            successes = self.default_successes
        if delay is None:
            delay = self.default_delay

        # Initialize wait state
        last_wait_state = JujuWaitState()
        noncompliant_wait_state = None
        success_count = 0
        start = datetime.now()

        # Begin wait loop
        while True:
            iteration_start = datetime.now()
            # With strict_timeout=False, allow continuation if we're making progress (ready=True)
            # With strict_timeout=True, always enforce timeout
            timeout_reached = iteration_start - start > timeout
            if timeout_reached and (strict_timeout or success_count == 0):
                break

            # Get current status
            status = self.status(model)

            # Check for error condition
            if error is not None:
                is_error, last_wait_state = error(status)
                if is_error:
                    raise JujuWaitTimeoutError(wait_state=last_wait_state)

            # Check for ready condition
            is_ready, last_wait_state = ready(status)
            if is_ready:
                success_count += 1
                if success_count >= successes:
                    return
            else:
                noncompliant_wait_state = last_wait_state
                success_count = 0

            # Wait before next iteration
            elapsed = datetime.now() - iteration_start
            time.sleep(max(0, (delay - elapsed).total_seconds()))

        # Timeout reached
        if noncompliant_wait_state is None:
            noncompliant_wait_state = dataclasses.replace(
                last_wait_state,
                insufficient_status_checks=True,
            )
        raise JujuWaitTimeoutError(wait_state=noncompliant_wait_state)

    def wait_idle(
        self,
        model: str,
        timeout: timedelta | None,
        count: int | None,
        strict_timeout: bool = False,
        applications: list[str] | None = None,
    ) -> None:
        if applications is None:
            applications = []
        self.wait(
            model,
            lambda status: all_statuses_are_in(
                status, *applications, application_statuses={"active"}, unit_statuses={"active"}
            ),
            timeout=timeout,
            successes=count,
            strict_timeout=strict_timeout,
        )

    def wait_application_settled(self, model: str, application: str, timeout: timedelta | None) -> None:
        self.wait(
            model,
            lambda status: all_statuses_are_in(
                status,
                application,
                application_statuses={"blocked", "active"},
                unit_statuses={"blocked", "active"},
                unit_agent_statuses={"idle"},
            ),
            timeout=timeout,
        )

    def wait_application_scaled(self, model: str, application: str, timeout: timedelta | None) -> None:
        self.wait(model, lambda status: applications_are_scaled(status, application), timeout=timeout)

    def wait_for_unit_message(self, model: str, unit: str, message: str, timeout: timedelta | None) -> None:
        self.wait(model, lambda status: units_have_message(message, status, unit), timeout=timeout)

    def wait_for_removal(self, model: str, applications: list[str], timeout: timedelta | None) -> None:
        self.wait(model, lambda status: applications_are_removed(status, *applications), timeout=timeout)

    def wait_for_removal_of_integration(
        self,
        model: str,
        endpoint_1: JujuIntegrationApplication,
        endpoint_2: JujuIntegrationApplication,
        timeout: timedelta | None,
    ) -> None:
        self.wait(model, lambda status: integrations_are_removed(status, (endpoint_1, endpoint_2)), timeout=timeout)

    def wait_for_removal_of_units(self, model: str, applications: list[str], timeout: timedelta | None) -> None:
        self.wait(model, lambda status: applications_have_no_units(status, *applications), timeout=timeout)

    def run_action(self, model: str, unit: str, action: str, params: dict[str, Any]) -> JujuTask:
        try:
            task = self.client.model(model).run(
                unit=unit,
                action=action,
                params=params,
            )
        except jubilant._task.TaskError as e:
            # Just extract the task from the exception
            task = e.task

        return JujuTask(task.id, task.return_code, task.status, task.message, task.results.get("output", ""))

    def add_secret(self, model: str, name: str, values: dict[str, str]) -> str:
        return (
            self.client.model(model)
            .add_secret(
                name=name,
                content=values,
            )
            .unique_identifier
        )

    def read_secret(self, model: str, name_or_id: str) -> dict[str, str]:
        # Call show secret
        show_secret_result = self.client.model(model).cli(
            "show-secret",
            name_or_id,
            "--reveal",
            "--format=yaml",
        )

        # There will only ever be one secret in the Juju result, and it is keyed by ID
        # We have `name_or_id`, but don't know which it might be, so we instead just get the first value
        return_value = next(iter(yaml.safe_load(show_secret_result).values()))["content"]
        if not isinstance(return_value, dict):
            raise TypeError(f"Expected secret content to be dict[str, str], got {type(return_value)}")
        return return_value

    def grant_secret(self, model: str, name_or_id: str, application: str) -> None:
        # Call grant secret
        self.client.model(model).cli(
            "grant-secret",
            name_or_id,
            application,
        )

    def remove_secret(self, model: str, name_or_id: str) -> None:
        # Call remove secret
        self.client.model(model).cli(
            "remove-secret",
            name_or_id,
        )

    def deploy_application(
        self,
        model: str,
        charm: str,
        application: str | None = None,
        config: dict[str, Any] | None = None,
        trust: bool = False,
    ) -> None:
        self.client.model(model).deploy(
            charm=charm,
            app=application,
            config=config,
            trust=trust,
        )

    def configure_application(self, model: str, application: str, values: dict[str, str]) -> None:
        self.client.model(model).config(
            app=application,
            values=values,
        )

    def get_application_config(self, model: str, application: str) -> dict[str, Any]:
        # I'd rather just pass this through, but to follow the return type correctly,
        # we'll convert to a dict.
        return {k: v for k, v in self.client.model(model).config(application).items()}

    def scp(self, model: str, source: str, destination: str) -> None:
        self.client.model(model).scp(
            source=source,
            destination=destination,
        )

    def ssh(self, model: str, application: str, command: str) -> None:
        self.client.model(model).ssh(
            target=application,
            command=command,
        )

    def unit_ip(self, model: str, unit: str) -> str:
        application, unit_id = unit.split("/")
        for possible_unit, unit_status in self.status(model).apps[application].units.items():
            _, possible_unit_id = possible_unit.split("/")
            if possible_unit_id == unit_id or (unit_id == "leader" and unit_status.leader):
                return unit_status.address
        raise KeyError(f"Unit '{unit}' not found")

    def list_applications(self, model: str) -> dict[str, JujuApplicationInfo]:
        return {
            app_name: JujuApplicationInfo(charm=app_info.charm, revision=app_info.charm_rev)
            for app_name, app_info in self.status(model).apps.items()
        }

    def list_integrations(self, model: str) -> set[JujuIntegration]:
        # Juju status yaml format doesn't expose provider/requirer information or
        # neighbor endpoint information, meaning the only way to get integrations
        # is by using the tabular format, which gives a complete picture.
        tabular_status = self.juju_status_text(model)
        integrations = set()
        in_integration_section = False
        for line in tabular_status.split("\n"):
            # Look for the integration section
            if line.startswith("Integration provider"):
                in_integration_section = True
                continue
            elif not in_integration_section:
                continue
            elif not line.strip():
                break

            # We are expecting 4 or 5 columns, the 5th one may have spaces, the first 4 shouldn't
            parts = re.match(
                r"(?P<provider>\S+)\s+(?P<requirer>\S+)\s+(?P<integration>\S+)\s+(?P<type>\S+)\s*(?P<message>.*)",
                line.strip(),
            )
            if parts is None:
                continue
            provider_str, requirer_str = parts.group("provider", "requirer")
            interface, integration_type = parts.group("integration", "type")

            # Skip peer integrations
            if integration_type == "peer":
                continue

            # Parse provider and requirer
            integrations.add(
                JujuIntegration(
                    provider=JujuIntegrationApplication.from_str(provider_str),
                    requirer=JujuIntegrationApplication.from_str(requirer_str),
                    interface=interface,
                )
            )
        return integrations

    def integration_exists(
        self, application_1: str, endpoint_1: str, application_2: str, endpoint_2: str, model: str
    ) -> bool:
        return {
            JujuIntegrationApplication(application=application_1, endpoint=endpoint_1),
            JujuIntegrationApplication(application=application_2, endpoint=endpoint_2),
        } in [{integration.provider, integration.requirer} for integration in self.list_integrations(model)]

    def version(self, model: str) -> str:
        return str(self.client.model(model).version())
