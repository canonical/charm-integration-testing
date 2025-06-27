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
