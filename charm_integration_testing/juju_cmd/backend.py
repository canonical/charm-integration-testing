# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import yaml

from charm_integration_testing.juju import JujuBackend, JujuWaitTimeoutError

from .cmd import CmdArg, CmdClient, CmdError
from .structures import JujuModel, JujuStatus


class JujuCmdBackend(JujuBackend):
    cmd_client: CmdClient

    def __init__(self, cmd_client: CmdClient = None):
        self.cmd_client = cmd_client if cmd_client is not None else CmdClient()

    def _call_juju(self, *args: list[CmdArg]) -> str:
        return self.cmd_client.call(CmdArg(value="juju"), *args)

    def _status(self, model: str) -> JujuStatus:
        return JujuStatus(
            **yaml.safe_load(
                self._call_juju(
                    CmdArg(value="status"),
                    CmdArg(name="model", value=model),
                    CmdArg(name="format", value="yaml"),
                )
            )
        )

    def is_k8s_model(self, model: str) -> bool:
        return self.show_model(model).type == "kubernetes"

    def show_model(self, model: str) -> JujuModel:
        return JujuModel(
            **(
                yaml.safe_load(
                    self._call_juju(
                        CmdArg(value="show-model"),
                        CmdArg(value=model),
                    )
                )[model]
            )
        )

    def scale_application(self, model: str, application: str, num: int):
        # Check if k8s model
        if self.is_k8s_model(model):
            # Call scale application
            self._call_juju(
                CmdArg(value="scale-application"),
                CmdArg(name="model", value=model),
                CmdArg(value=application),
                CmdArg(value=num),
            )
        else:
            # Get current juju units
            units = sorted(
                self._status(model).applications[application].units.keys(), key=lambda unit: unit.split("/", 1)[1]
            )

            # Add or remove units
            if len(units) < num:
                self._call_juju(
                    CmdArg(value="add-unit"),
                    CmdArg(name="model", value=model),
                    CmdArg(value=application),
                    CmdArg(value=num - len(units), name="num-units"),
                )
            elif len(units) > num:
                self._call_juju(
                    CmdArg(value="remove-unit"),
                    CmdArg(name="model", value=model),
                    CmdArg(name="no-prompt"),
                    *[CmdArg(value=unit) for unit in units[num:]],
                )

    def num_units(self, model: str, application: str) -> int:
        return len(self._status(model).applications[application].units)

    def _wait_for(self, model: str, query: str, timeout: timedelta):
        try:
            self._call_juju(
                CmdArg(value="wait-for"),
                CmdArg(value="model"),
                CmdArg(value=model),
                CmdArg(name="query", value=query),
                CmdArg(name="timeout", value=f"{timeout.total_seconds()}s"),
            )
        except CmdError as e:
            if "ERROR timed out waiting for" in e.stderr:
                raise JujuWaitTimeoutError
            else:
                raise e

    def wait_idle(self, model: str, timeout: timedelta):
        self._wait_for(
            model,
            "len(applications) == 0 || (forEach(applications, app => app.status == 'active') && forEach(units, unit => unit.workload-status == 'active' && unit.agent-status == 'idle'))",
            timeout,
        )

    def juju_status_text(self, model: str) -> str:
        return self._call_juju(
            CmdArg(value="status"),
            CmdArg(name="model", value=model),
            CmdArg(name="integrations"),
        )

    def integrate(self, model: str, target_1: str, target_2: str):
        self._call_juju(
            CmdArg(value="integrate"),
            CmdArg(name="model", value=model),
            CmdArg(value=target_1),
            CmdArg(value=target_2),
        )

    def remove_integration(self, model: str, target_1: str, target_2: str):
        self._call_juju(
            CmdArg(value="remove-relation"),
            CmdArg(name="model", value=model),
            CmdArg(value=target_1),
            CmdArg(value=target_2),
        )

    def deploy_bundle_file(self, model: str, bundle: str):
        self._call_juju(
            CmdArg(value="deploy"),
            CmdArg(name="model", value=model),
            CmdArg(name="trust"),
            CmdArg(value=f"./{bundle}"),
        )

    def remove_applications(self, model: str, *applications: list[str]):
        self._call_juju(
            CmdArg(value="remove-application"),
            CmdArg(name="model", value=model),
            CmdArg(name="no-prompt"),
            *[CmdArg(value=application) for application in applications],
        )

    def wait_for_removal(self, model: str, applications: list[str], timeout: timedelta):
        # Checking unit.application instead of application.name due to Juju bug: https://github.com/juju/juju/issues/18785
        name_checks = " && ".join([f"unit.application != '{application}'" for application in applications])
        self._wait_for(model, f"len(applications) == 0 || forEach(units, unit => {name_checks})", timeout)
