# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


import time
from datetime import datetime, timedelta
from typing import Callable

import jubilant
import yaml
from juju import JujuIntegrationApplication, JujuWaitState, JujuWaitTimeoutError
from juju_cmd import JujuCmdBackend

from .client import JubilantClient
from .wait import (
    all_statuses_are_in,
    applications_are_removed,
    applications_are_scaled,
    applications_have_no_units,
    get_integrations,
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

    def wait(
        self,
        model: str,
        ready: Callable[[jubilant.Status], tuple[bool, JujuWaitState]],
        error: Callable[[jubilant.Status], tuple[bool, JujuWaitState]] | None = None,
        timeout: timedelta | None = None,
        successes: int | None = None,
        delay: timedelta | None = None,
    ):
        if timeout is None:
            timeout = self.default_timeout
        if successes is None:
            successes = self.default_successes
        if delay is None:
            delay = self.default_delay

        wait_state = None
        success_count = 0
        start = datetime.now()

        while (datetime.now() - start) < timeout:
            status = self.client.model(model).status()

            if error is not None:
                is_error, wait_state = error(status)
                if is_error:
                    raise JujuWaitTimeoutError(wait_state=wait_state)

            is_ready, wait_state = ready(status)
            if is_ready:
                success_count += 1
                if success_count >= successes:
                    return
            else:
                success_count = 0

            time.sleep(delay.total_seconds())

        raise JujuWaitTimeoutError(wait_state=wait_state)

    def wait_idle(self, model: str, timeout: timedelta | None, count: int):
        self.wait(
            model,
            lambda status: all_statuses_are_in({"active"}, status),
            timeout=timeout,
            successes=count,
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

    def integration_exists(
        self, application_1: str, endpoint_1: str, application_2: str, endpoint_2: str, model: str
    ) -> bool:
        status = self.client.model(model).status()
        integrations = get_integrations(status)

        target_applications = {
            JujuIntegrationApplication(application_1, endpoint_1),
            JujuIntegrationApplication(application_2, endpoint_2),
        }

        return target_applications in {integration.applications for integration in integrations}
