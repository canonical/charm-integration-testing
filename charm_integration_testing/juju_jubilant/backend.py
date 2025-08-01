# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta
from typing import Callable

import jubilant
import yaml
from juju import JujuIntegrationApplication, JujuWaitState, JujuWaitTimeoutError
from juju_cmd import JujuCmdBackend

from .client import JubilantClient
from .wait import (
    WaitMonitor,
    all_statuses_are_in,
    applications_are_removed,
    applications_are_scaled,
    applications_have_no_units,
    integrations_are_removed,
    units_have_message,
)


class JubilantBackend(JujuCmdBackend):
    client: JubilantClient

    def __init__(self, client: JubilantClient | None = None):
        super().__init__()
        self.client = client or JubilantClient()

    def wait(
        self,
        model: str,
        ready: Callable[[jubilant.Status], tuple[bool, JujuWaitState]],
        error: Callable[[jubilant.Status], tuple[bool, JujuWaitState]] | None = None,
        timeout: timedelta | None = None,
        period: timedelta | None = None,
        delay: int = 1,
        **kwargs,
    ):
        wait_monitor = WaitMonitor(ready=ready, error=error)
        try:
            return self.client.model(model).wait(
                ready=wait_monitor.ready,
                error=wait_monitor.error,
                timeout=timeout.total_seconds() if timeout else None,
                successes=int(period.total_seconds()) if period else 1,
                delay=delay,
                **kwargs,
            )
        except TimeoutError:
            raise JujuWaitTimeoutError(wait_state=wait_monitor.last_noncompliant_wait_state)

    def wait_idle(self, model: str, timeout: timedelta | None, period: timedelta | None):
        self.wait(
            model,
            lambda status: all_statuses_are_in({"active"}, status),
            timeout=timeout,
            period=period,
        )

    def wait_application_settled(self, model: str, application: str, timeout: timedelta | None):
        self.wait(
            model, lambda status: all_statuses_are_in({"blocked", "active"}, status, application), timeout=timeout
        )

    def wait_application_scaled(self, model: str, application: str, timeout: timedelta | None):
        self.wait(model, lambda status: applications_are_scaled(status, application), timeout=timeout)

    def wait_for_unit_message(self, model: str, unit: str, message: str, timeout: timedelta | None):
        self.wait(model, lambda status: units_have_message(message, status, unit), timeout=timeout)

    def wait_for_removal(self, model: str, applications: list[str], timeout: timedelta | None):
        self.wait(model, lambda status: applications_are_removed(status, *applications), timeout=timeout)

    def wait_for_removal_of_integration(
        self,
        model: str,
        endpoint_1: JujuIntegrationApplication,
        endpoint_2: JujuIntegrationApplication,
        timeout: timedelta | None,
    ):
        self.wait(model, lambda status: integrations_are_removed(status, (endpoint_1, endpoint_2)), timeout=timeout)

    def wait_for_removal_of_units(self, model: str, applications: list[str], timeout: timedelta | None):
        self.wait(model, lambda status: applications_have_no_units(status, *applications), timeout=timeout)

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
        result = self.client.model(model).cli(
            "show-secret",
            name_or_id,
            "--reveal",
            "--format=yaml",
        )

        # There will only ever be one secret in the Juju result, and it is keyed by ID
        # We have `name_or_id`, but don't know which it might be, so we instead just get the first value
        return next(iter(yaml.safe_load(result).values()))["content"]

    def grant_secret(self, model: str, name_or_id: str, application: str):
        # Call grant secret
        self.client.model(model).cli(
            "grant-secret",
            name_or_id,
            application,
        )

    def remove_secret(self, model: str, name_or_id: str):
        # Call remove secret
        self.client.model(model).cli(
            "remove-secret",
            name_or_id,
        )

    def deploy_application(self, model: str, charm: str, application: str | None = None):
        self.client.model(model).deploy(
            charm=charm,
            app=application,
        )

    def configure_application(self, model: str, application: str, values: dict[str, str]):
        self.client.model(model).config(
            app=application,
            values=values,
        )

    def scp(self, model: str, source: str, destination: str):
        self.client.model(model).scp(
            source=source,
            destination=destination,
        )

    def ssh(self, model: str, application: str, command: str):
        self.client.model(model).ssh(
            target=application,
            command=command,
        )

    def unit_ip(self, model: str, unit: str) -> str:
        application, unit_id = unit.split("/")
        for possible_unit, unit_status in self.client.model(model).status().apps[application].units.items():
            _, possible_unit_id = possible_unit.split("/")
            if possible_unit_id == unit_id or (unit_id == "leader" and unit_status.leader):
                return unit_status.address
        raise KeyError(f"Unit '{unit}' not found")

    def get_charm_revisions(self, model: str) -> set[tuple[str, int]]:
        return {(app_info.charm, app_info.charm_rev) for app_info in self.client.model(model).status().apps.values()}
