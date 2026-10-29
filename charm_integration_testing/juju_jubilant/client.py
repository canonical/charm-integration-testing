# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import jubilant
from juju import JujuModelHandle

_KILL_CONTROLLER_TIMEOUT_SECONDS = 120

_LONG_RUNNING_COMMANDS = frozenset(
    {
        "add-k8s",
        "add-model",
        "bootstrap",
        "deploy",
        "destroy-controller",
        "destroy-model",
        "exec",
        "kill-controller",
        "migrate",
        "refresh",
        "run",
        "scp",
        "ssh",
        "upgrade-controller",
        "upgrade-model",
        "wait-for",
    }
)


class _BoundedJuju(jubilant.Juju):
    query_timeout: float

    def _cli(
        self,
        *args: str,
        include_model: bool = True,
        stdin: str | None = None,
        log: bool = True,
        timeout: float | None = None,
    ) -> tuple[str, str]:
        # Jubilant's public cli()/status() do not expose a subprocess timeout.
        if timeout is None:
            if args[0] == "kill-controller":
                timeout = _KILL_CONTROLLER_TIMEOUT_SECONDS
            else:
                timeout = self.wait_timeout if args[0] in _LONG_RUNNING_COMMANDS else self.query_timeout
        return super()._cli(
            *args,
            include_model=include_model,
            stdin=stdin,
            log=log,
            timeout=timeout,
        )


class JubilantClient:
    def __init__(self, query_timeout: timedelta = timedelta(seconds=60)) -> None:
        if query_timeout <= timedelta(0):
            raise ValueError("Query timeout must be positive.")
        self.query_timeout = query_timeout

    def model(self, model: JujuModelHandle | None) -> jubilant.Juju:
        client = _BoundedJuju(
            model=model.uri if model is not None else None,
            wait_timeout=timedelta(days=1).total_seconds(),
        )
        client.query_timeout = self.query_timeout.total_seconds()
        return client
