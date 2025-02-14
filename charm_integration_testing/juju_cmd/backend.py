# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import yaml
from juju import JujuBackend, JujuIntegration, JujuIntegrationApplication, JujuWaitTimeoutError

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

    def list_applications(self, model: str) -> set[str]:
        return set(self._status(model).applications.keys())

    def list_integrations(self, model: str) -> set[JujuIntegration]:
        status = self._status(model)
        return {
            JujuIntegration(
                interface=integration_1.interface,
                applications=frozenset(
                    {
                        JujuIntegrationApplication(application_1, endpoint_1),
                        JujuIntegrationApplication(application_2, endpoint_2),
                    }
                ),
            )
            # Iterate over every integration on every endpoint
            for application_1, application_1_info in status.applications.items()
            for endpoint_1, integrations_1 in application_1_info.integrations.items()
            for integration_1 in integrations_1
            # Then for each also iterate over every integration on every endpoint again
            for application_2, application_2_info in status.applications.items()
            for endpoint_2, integrations_2 in application_2_info.integrations.items()
            for integration_2 in integrations_2
            # Then for all check if the integrations complete a pair
            if integration_1.interface == integration_2.interface
            and application_1 == integration_2.integrated_application
            and application_2 == integration_1.integrated_application
        }

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

    def integrate(self, model: str, target_1: JujuIntegrationApplication, target_2: JujuIntegrationApplication):
        self._call_juju(
            CmdArg(value="integrate"),
            CmdArg(name="model", value=model),
            CmdArg(value=str(target_1)),
            CmdArg(value=str(target_2)),
        )

    def remove_integration(
        self, model: str, target_1: JujuIntegrationApplication, target_2: JujuIntegrationApplication
    ):
        self._call_juju(
            CmdArg(value="remove-relation"),
            CmdArg(name="model", value=model),
            CmdArg(value=str(target_1)),
            CmdArg(value=str(target_2)),
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
