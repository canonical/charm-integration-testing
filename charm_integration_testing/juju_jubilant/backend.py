# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import jubilant
import yaml
from juju import JujuWaitTimeoutError
from juju_cmd import JujuCmdBackend


class JubilantClient:
    def model(self, model: str) -> jubilant.Juju:
        return jubilant.Juju(
            model=model,
            wait_timeout=timedelta(days=1).total_seconds(),
        )


class JubilantBackend(JujuCmdBackend):
    client: JubilantClient

    def __init__(self, client: JubilantClient | None = None):
        super().__init__()
        self.client = client or JubilantClient()

    def wait_idle(self, model: str, timeout: timedelta | None, period: timedelta | None):
        try:
            self.client.model(model).wait(
                jubilant.all_active,
                error=None,
                timeout=timeout.total_seconds() if timeout else None,
                successes=int(period.total_seconds()) if period else 1,
                delay=1,
            )
        except TimeoutError:
            raise JujuWaitTimeoutError

    @staticmethod
    def _all_statuses_are_in(expected: set[str], status: jubilant.Status, application: str) -> bool:
        application_info = status.apps.get(application)
        if application_info is None:
            return False
        if application_info.app_status.current not in expected:
            return False
        for unit_info in status.get_units(application).values():
            if unit_info.workload_status.current not in expected:
                return False
        return True

    def wait_application_settled(self, model: str, application: str, timeout: timedelta | None):
        try:
            self.client.model(model).wait(
                lambda status: self._all_statuses_are_in({"blocked", "active"}, status, application),
                timeout=timeout.total_seconds() if timeout else None,
                delay=1,
            )
        except TimeoutError:
            raise JujuWaitTimeoutError

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
