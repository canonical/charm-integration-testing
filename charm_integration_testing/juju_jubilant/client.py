# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import jubilant
from juju import JujuModelHandle


# TODO: Remove once our Jubilant dependency provides equivalent subprocess limits:
# https://github.com/canonical/jubilant/issues/443
class _BoundedJuju(jubilant.Juju):
    command_timeout: float
    bootstrap_timeout: float

    def _cli(
        self,
        *args: str,
        include_model: bool = True,
        stdin: str | None = None,
        log: bool = True,
        timeout: float | None = None,
    ) -> tuple[str, str]:
        # Bound connection stalls that Juju/Jubilant's operation waits do not cover.
        # Bootstrap provisions a machine and agent, so it gets a longer ceiling.
        ceiling = self.bootstrap_timeout if args[:1] == ("bootstrap",) else self.command_timeout
        timeout = ceiling if timeout is None else min(timeout, ceiling)
        return super()._cli(
            *args,
            include_model=include_model,
            stdin=stdin,
            log=log,
            timeout=timeout,
        )


class JubilantClient:
    def __init__(
        self,
        command_timeout: timedelta = timedelta(minutes=5),
        bootstrap_timeout: timedelta = timedelta(minutes=15),
    ) -> None:
        if command_timeout <= timedelta(0) or bootstrap_timeout <= timedelta(0):
            raise ValueError("Command timeouts must be positive.")
        self.command_timeout = command_timeout
        self.bootstrap_timeout = bootstrap_timeout

    def model(self, model: JujuModelHandle | None) -> jubilant.Juju:
        client = _BoundedJuju(
            model=model.uri if model is not None else None,
            wait_timeout=timedelta(days=1).total_seconds(),
        )
        client.command_timeout = self.command_timeout.total_seconds()
        client.bootstrap_timeout = self.bootstrap_timeout.total_seconds()
        return client
