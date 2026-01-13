# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


import os
import time
from datetime import datetime, timedelta, timezone

import yaml
from juju import JujuBackend, JujuExecOutput, JujuIntegration, JujuIntegrationApplication, JujuWaitTimeoutError
from juju.backend import JujuStatusPerformanceWarning, warn_performance

from .cmd import CmdArg, CmdClient, CmdError
from .structures import JujuExecTask, JujuModel, JujuSecretInfo, JujuStatus


class JujuCmdBackend(JujuBackend):
    cmd_client: CmdClient

    def __init__(self, cmd_client: CmdClient | None = None):
        self.cmd_client = cmd_client if cmd_client is not None else CmdClient()

    def _call_juju(self, *args: CmdArg) -> str:
        return self.cmd_client.call(CmdArg(value="juju"), *args)

    @warn_performance(category=JujuStatusPerformanceWarning, threshold=timedelta(seconds=5))
    def _status(self, model: str, *selectors: str) -> JujuStatus:
        return JujuStatus(
            **yaml.safe_load(
                self._call_juju(
                    CmdArg(value="status"),
                    CmdArg(name="model", value=model),
                    CmdArg(name="format", value="yaml"),
                    *[CmdArg(value=selector) for selector in selectors],
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

    def scale_application(self, model: str, application: str, num: int) -> None:
        # Check if k8s model
        if self.is_k8s_model(model):
            # Call scale application
            self._call_juju(
                CmdArg(value="scale-application"),
                CmdArg(name="model", value=model),
                CmdArg(value=application),
                CmdArg(value=str(num)),
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
                    CmdArg(value=str(num - len(units)), name="num-units"),
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

    def _wait_for(self, model: str, scope: str, specifier: str, query: str, timeout: timedelta | None) -> None:
        try:
            self._call_juju(
                CmdArg(value="wait-for"),
                CmdArg(value=scope),
                CmdArg(value=specifier),
                CmdArg(name="model", value=model) if scope != "model" else CmdArg(),
                CmdArg(name="query", value=query),
                *([CmdArg(name="timeout", value=f"{timeout.total_seconds()}s")] if timeout is not None else []),
            )
        except CmdError as e:
            if "ERROR timed out waiting for" in e.stderr:
                raise JujuWaitTimeoutError
            else:
                raise e

    def wait_application_settled(self, model: str, application: str, timeout: timedelta | None) -> None:
        unit_workload_status_settled = " || ".join(
            {f"unit.workload-status == '{status}'" for status in {"active", "blocked"}}
        )
        unit_agent_status_settled = " || ".join({f"unit.agent-status == '{status}'" for status in {"idle", "failed"}})
        self._wait_for(
            model,
            "application",
            application,
            f"len(units) == 0 || forEach(units, unit => ({unit_workload_status_settled}) && ({unit_agent_status_settled}))",
            timeout,
        )

    def wait_application_scaled(self, model: str, application: str, timeout: timedelta | None) -> None:
        # Wait for an application to reach it's desired scale
        # See https://github.com/juju/juju/blob/add3443726e40faebaba0103289c6660251fa1eb/cmd/juju/status/formatted.go#L239
        start_time = datetime.now(timezone.utc)
        while timeout is None or datetime.now(timezone.utc) < start_time + timeout:
            # Get application from juju status
            application_status = self._status(model).applications[application]

            # Get number of units idle or executing
            num_units = len(
                [
                    unit
                    for unit in application_status.units.values()
                    if unit.juju_status.current in {"idle", "executing"}
                ]
            )

            # Compare with the target scale of the application
            if application_status.scale == num_units:
                return

            time.sleep(0.05)

        raise JujuWaitTimeoutError

    def wait_for_unit_message(self, model: str, unit: str, message: str, timeout: timedelta | None) -> None:
        # Loop until timeout
        start_time = datetime.now(timezone.utc)
        while timeout is None or datetime.now(timezone.utc) < start_time + timeout:
            # Find unit in juju status
            juju_status = self._status(model, unit)
            application_info = next(iter(juju_status.applications.values()), None)
            unit_info = next(iter(application_info.units.values()), None) if application_info else None

            # Check the application message
            if unit_info and message.lower() in unit_info.workload_status.message.lower():
                return

            # Wait for a bit
            time.sleep(timedelta(seconds=1).total_seconds())

        raise JujuWaitTimeoutError

    def juju_status_text(self, model: str) -> str:
        return self._call_juju(
            CmdArg(value="status"),
            CmdArg(name="model", value=model),
            CmdArg(name="integrations"),
        )

    def integrate(self, model: str, target_1: JujuIntegrationApplication, target_2: JujuIntegrationApplication) -> None:
        self._call_juju(
            CmdArg(value="integrate"),
            CmdArg(name="model", value=model),
            CmdArg(value=str(target_1)),
            CmdArg(value=str(target_2)),
        )

    def remove_integration(
        self, model: str, target_1: JujuIntegrationApplication, target_2: JujuIntegrationApplication
    ) -> None:
        self._call_juju(
            CmdArg(value="remove-relation"),
            CmdArg(name="model", value=model),
            CmdArg(value=str(target_1)),
            CmdArg(value=str(target_2)),
        )

    def deploy_bundle_file(self, model: str, bundle: str) -> None:
        if not os.path.isfile(bundle):
            raise ValueError(f"Bundle file '{bundle}' not found.")
        self._call_juju(
            CmdArg(value="deploy"),
            CmdArg(name="model", value=model),
            CmdArg(name="trust"),
            CmdArg(value=bundle),
        )

    def remove_applications(self, model: str, *applications: str) -> None:
        self._call_juju(
            CmdArg(value="remove-application"),
            CmdArg(name="model", value=model),
            CmdArg(name="no-prompt"),
            *[CmdArg(value=application) for application in applications],
        )

    def wait_for_removal(self, model: str, applications: list[str], timeout: timedelta | None) -> None:
        # Juju bug causes panic: https://github.com/juju/juju/issues/18785
        # name_checks = " && ".join([f"application.name != '{application}'" for application in applications])
        # self._wait_for(model, "model", model, f"len(applications) == 0 || forEach(applications, application => {name_checks})", timeout)

        # Check status for application until Juju bug is fixed
        start_time = datetime.now(timezone.utc)
        while timeout is None or datetime.now(timezone.utc) < start_time + timeout:
            # Check if any of the applications exist
            if not (set(applications) & self.list_applications(model=model)):
                return

            time.sleep(0.05)

        raise JujuWaitTimeoutError

    def wait_for_removal_of_integration(
        self,
        model: str,
        target_1: JujuIntegrationApplication,
        target_2: JujuIntegrationApplication,
        timeout: timedelta | None,
    ) -> None:
        # Juju wait-for for doesn't support integration, so just check juju status
        start_time = datetime.now(timezone.utc)
        while timeout is None or datetime.now(timezone.utc) < start_time + timeout:
            # Check if the integration exists
            if not any(
                [
                    ({target_1, target_2} & integration.applications)
                    for integration in self.list_integrations(model=model)
                ]
            ):
                return

            time.sleep(0.05)

        raise JujuWaitTimeoutError

    def wait_for_removal_of_units(self, model: str, applications: list[str], timeout: timedelta | None) -> None:
        name_checks = " && ".join([f"unit.application != '{application}'" for application in applications])
        self._wait_for(
            model, "model", model, f"len(applications) == 0 || forEach(units, unit => {name_checks})", timeout
        )

    def application_charm(self, model: str, application: str) -> str | None:
        return self._status(model).applications[application].charm

    def application_units(self, model: str, application: str) -> list[str]:
        return list(self._status(model).applications[application].units.keys())

    def _exec(self, model: str, task: str, unit: str | None = None) -> dict[str, JujuExecOutput]:
        # Call juju exec
        try:
            exec_output = self._call_juju(
                CmdArg(value="exec"),
                CmdArg(name="model", value=model),
                CmdArg(name="unit", value=unit) if unit else CmdArg(),
                CmdArg(name="format", value="yaml"),
                CmdArg(value="--"),
                CmdArg(value=task),
            )
        except CmdError as e:
            if "ERROR the following task failed" in e.stderr:
                exec_output = e.stdout
            else:
                raise e

        # Parse output
        parsed_output = {unit: JujuExecTask(**result) for unit, result in yaml.safe_load(exec_output).items()}

        # Return expected output
        return {
            unit: JujuExecOutput(
                return_code=task.results.return_code,
                stdout=task.results.stdout,
                stderr=task.results.stderr,
            )
            for unit, task in parsed_output.items()
        }

    def exec_unit(self, model: str, unit: str, task: str) -> JujuExecOutput:
        # Call exec
        exec_output = self._exec(model, task, unit=unit)

        # Return unit stdout
        return next(iter(exec_output.values()))

    def add_secret(self, model: str, name: str, values: dict[str, str]) -> str:
        raise NotImplementedError

    def read_secret(self, model: str, name_or_id: str) -> dict[str, str]:
        # Read the secret
        result = self._call_juju(
            CmdArg(value="show-secret"),
            CmdArg(name="model", value=model),
            CmdArg(value=name_or_id),
            CmdArg(name="reveal"),
            CmdArg(name="format", value="yaml"),
        )

        # Parse response
        content_dict = JujuSecretInfo(**next(iter(yaml.safe_load(result).values()))).content
        if content_dict is None:
            raise ValueError(f"Secret '{name_or_id}' has no content.")
        return content_dict

    def grant_secret(self, model: str, name_or_id: str, application: str) -> None:
        # Authorize the application
        self._call_juju(
            CmdArg(value="grant-secret"),
            CmdArg(name="model", value=model),
            CmdArg(value=name_or_id),
            CmdArg(value=application),
        )

    def run_action(self, model: str, unit: str, action: str, arguments: dict[str, str]) -> None:
        # Run the action on the unit
        self._call_juju(
            CmdArg(value="run"),
            CmdArg(name="model", value=model),
            CmdArg(value=unit),
            CmdArg(value=action),
            *[CmdArg(value=f"{key}={value}") for key, value in arguments.items()],
        )

    def remove_secret(self, model: str, name_or_id: str) -> None:
        raise NotImplementedError

    def version(self, model: str) -> str:
        raise NotImplementedError
