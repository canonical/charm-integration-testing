# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import yaml

from charm_integration_testing.juju import JujuClient

from .cmd import CmdArg, CmdClient
from .structures import JujuStatus


class JujuCmdClient(JujuClient):
    cmd_client: CmdClient

    def __init__(self, cmd_client: CmdClient = None):
        self.cmd_client = cmd_client if cmd_client is not None else CmdClient()

    def _call_juju(self, *args: list[CmdArg]) -> str:
        return self.cmd_client.call(CmdArg(value="juju"), *args)

    def _status(self) -> JujuStatus:
        return JujuStatus(
            **yaml.safe_load(
                self._call_juju(
                    CmdArg(value="status"),
                    CmdArg(value="yaml", name="format"),
                )
            )
        )

    def scale_application(self, application: str, num: int):
        # Get current juju units
        units = sorted(self._status().applications[application].units.keys(), key=lambda unit: unit.split("/", 1)[1])

        # Add or remove units
        # juju scale-application does not work with VM charms XXX: SQT-431
        self._call_juju(
                CmdArg(value="scale-application")
                CmdArg(value=application)
                CmdArg(name="num-units"),
                CmdArg(value=num)
                )
        # if len(units) < num:
        #     self._call_juju(
        #         CmdArg(value="add-unit"),
        #         CmdArg(value=application),
        #         CmdArg(value=num - len(units), name="num-units"),
        #     )
        # elif len(units) > num:
        #     self._call_juju(
        #         CmdArg(value="remove-unit"),
        #         CmdArg(name="no-prompt"),
        #         *[CmdArg(value=unit) for unit in units[num:]],
        #     )

    def num_units(self, application: str) -> int:
        return len(self._status().applications[application].units)

    def wait_idle(self, model: str = "default", timeout: timedelta = timedelta(days=1)) -> bool:
        self._call_juju(
            CmdArg(value="wait-for"),
            CmdArg(value="model"),
            CmdArg(value=model),
            CmdArg(
                name="query",
                value="forEach(applications, app => app.status == 'active') && forEach(units, unit => unit.workload-status == 'active' && unit.agent-status == 'idle')",
            ),
            CmdArg(name="timeout", value=f"{timeout.total_seconds()}s"),
        )
