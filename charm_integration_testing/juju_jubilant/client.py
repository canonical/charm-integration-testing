# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


import subprocess
from datetime import timedelta

import jubilant
from juju import JujuModelHandle


class JubilantClient:
    def ssh(self, model: JujuModelHandle, machine: str, command: str, timeout: float) -> str:
        """Run SSH with a process timeout, including Juju's connection setup."""
        juju = self.model(model)
        args = [
            juju.cli_binary,
            "ssh",
            "--model",
            model.uri,
            machine,
            command,
        ]
        try:
            return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout
        except subprocess.CalledProcessError as error:
            raise jubilant.CLIError(error.returncode, error.cmd, error.stdout, error.stderr) from None

    def model(self, model: JujuModelHandle | None) -> jubilant.Juju:
        return jubilant.Juju(
            model=model.uri if model is not None else None,
            wait_timeout=timedelta(days=1).total_seconds(),
        )
