# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


from typing import Callable, Iterator

import jubilant
from juju import JujuApplicationState, JujuIntegration, JujuIntegrationApplication, JujuUnitState, JujuWaitState


class WaitMonitor:
    # There are cases where charms have errors, then go active idle, then go to error again.
    # If the test times out when the charm is active idle, it's hard to tell what the error was.
    # This works by saving the last state where the wait condition wasn't met.
    last_noncompliant_wait_state: JujuWaitState

    call_ready: Callable[[jubilant.Status], tuple[bool, JujuWaitState]]
    call_error: Callable[[jubilant.Status], tuple[bool, JujuWaitState]] | None

    def __init__(
        self,
        ready: Callable[[jubilant.Status], tuple[bool, JujuWaitState]],
        error: Callable[[jubilant.Status], tuple[bool, JujuWaitState]] | None = None,
    ):
        self.last_noncompliant_wait_state = JujuWaitState()
        self.call_ready = ready
        self.call_error = error

    def ready(self, status: jubilant.Status) -> bool:
        # Check ready and save application state
        ready, wait_state = self.call_ready(status)
        if not ready:
            self.last_noncompliant_wait_state = wait_state
        return ready

    def error(self, status: jubilant.Status) -> bool:
        # Check error and save application state
        if self.call_error is None:
            return False
        error, wait_state = self.call_error(status)
        if error:
            self.last_noncompliant_wait_state = wait_state
        return error


def get_unit_info(status: jubilant.Status, unit: str) -> jubilant.statustypes.UnitStatus | None:
    application, unit_id = unit.split("/")
    for possible_unit, unit_info in status.get_units(application).items():
        if possible_unit == unit:
            return unit_info
        if unit_id == "leader" and unit_info.leader:
            return unit_info
    return None


def generate_endpoint_integrations(status: jubilant.Status) -> Iterator[tuple[str, str, str]]:
    for application, application_info in status.apps.items():
        for endpoint, integrations in application_info.relations.items():
            for integration in integrations:
                yield (application, endpoint, integration)


def get_integrations(status: jubilant.Status) -> set[JujuIntegration]:
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
        for application_1, endpoint_1, integration_1 in generate_endpoint_integrations(status)
        # Then for each also iterate over every integration on every endpoint again
        for application_2, endpoint_2, integration_2 in generate_endpoint_integrations(status)
        # Then for all check if the integrations complete a pair
        if integration_1.interface == integration_2.interface
        and application_1 == integration_2.related_app
        and application_2 == integration_1.related_app
    }


def get_application_state(status: jubilant.Status, application: str) -> JujuApplicationState:
    application_info = status.apps[application]
    return JujuApplicationState(
        charm=application_info.charm,
        revision=application_info.charm_rev,
        status=application_info.app_status.current,
        message=application_info.app_status.message,
    )


def get_unit_state(status: jubilant.Status, unit: str) -> JujuUnitState:
    application, _ = unit.split("/")
    application_info = status.apps[application]
    unit_info = get_unit_info(status, unit)
    return JujuUnitState(
        charm=application_info.charm,
        revision=application_info.charm_rev,
        status=unit_info.workload_status.current,
        message=unit_info.workload_status.message,
    )


def all_statuses_are_in(expected: set[str], status: jubilant.Status, *applications: str) -> tuple[bool, JujuWaitState]:
    if not applications:
        applications = status.apps.keys()

    noncompliant_applications = {}
    noncompliant_units = {}
    for application in applications:
        if application not in status.apps:
            noncompliant_applications[application] = None
            continue
        application_info = status.apps[application]
        if application_info.app_status.current not in expected:
            noncompliant_applications[application] = get_application_state(status, application)
        for unit, unit_info in status.get_units(application).items():
            if unit_info.workload_status.current not in expected:
                noncompliant_units[unit] = get_unit_state(status, unit)

    is_compliant = (len(noncompliant_applications) == 0) and (len(noncompliant_units) == 0)
    return is_compliant, JujuWaitState(
        message=f"waiting for application to reach [{', '.join(sorted(expected))}]",
        noncompliant_applications=noncompliant_applications,
        noncompliant_units=noncompliant_units,
    )


def applications_are_scaled(status: jubilant.Status, *applications: str) -> tuple[bool, JujuWaitState]:
    # Check applications have reached desired scale
    # See https://github.com/juju/juju/blob/add3443726e40faebaba0103289c6660251fa1eb/cmd/juju/status/formatted.go#L239
    if not applications:
        applications = status.apps.keys()

    noncompliant_applications = {}
    noncompliant_units = {}
    for application in applications:
        # Get application from juju status
        if application not in status.apps:
            noncompliant_applications[application] = None
            continue
        application_info = status.apps[application]

        # Get number of units idle or executing
        valid_units = []
        for unit, unit_info in status.get_units(application).items():
            if unit_info.juju_status.current in {"idle", "executing"}:
                valid_units.append(unit)
            else:
                noncompliant_units[unit] = get_unit_state(status, unit)

        # Compare with the target scale of the application
        if application_info.scale != len(valid_units):
            noncompliant_applications[application] = get_application_state(status, application)

    is_compliant = (len(noncompliant_applications) == 0) and (len(noncompliant_units) == 0)
    return is_compliant, JujuWaitState(
        message="waiting for application to scale",
        noncompliant_applications=noncompliant_applications,
        noncompliant_units=noncompliant_units,
    )


def units_have_message(message: str, status: jubilant.Status, *units: str) -> tuple[bool, JujuWaitState]:
    if not units:
        units = (unit for application in status.apps for unit in status.get_units(application))

    noncompliant_units = {}
    for unit in units:
        unit_info = get_unit_info(status, unit)
        if unit_info is None:
            noncompliant_units[unit] = None
            continue
        if message.lower() not in unit_info.workload_status.message.lower():
            noncompliant_units[unit] = get_unit_state(status, unit)

    return len(noncompliant_units) == 0, JujuWaitState(
        message=f"waiting for unit message '{message}'",
        noncompliant_units=noncompliant_units,
    )


def applications_are_removed(status: jubilant.Status, *applications: str) -> tuple[bool, JujuWaitState]:
    if not applications:
        applications = status.apps.keys()

    noncompliant_applications = {}
    for application in applications:
        if application in status.apps:
            noncompliant_applications[application] = get_application_state(status, application)

    return len(noncompliant_applications) == 0, JujuWaitState(
        message="waiting for application removal",
        noncompliant_applications=noncompliant_applications,
    )


def integrations_are_removed(
    status: jubilant.Status, *integrations: tuple[JujuIntegrationApplication, JujuIntegrationApplication]
) -> tuple[bool, JujuWaitState]:
    existing_integrations = {integration.applications for integration in get_integrations(status)}
    if not integrations:
        integrations = existing_integrations

    noncompliant_applications = {}
    for integration in integrations:
        if set(integration) in existing_integrations:
            for endpoint in integration:
                noncompliant_applications[endpoint.application] = get_application_state(status, endpoint.application)

    return len(noncompliant_applications) == 0, JujuWaitState(
        message="waiting for integration removal",
        noncompliant_applications=noncompliant_applications,
    )


def applications_have_no_units(status: jubilant.Status, *applications: str) -> tuple[bool, JujuWaitState]:
    if not applications:
        applications = status.apps.keys()

    noncompliant_applications = {}
    noncompliant_units = {}
    for application in applications:
        if application not in status.apps:
            noncompliant_applications[application] = None
            continue
        units = status.get_units(application)
        if len(units) > 0:
            noncompliant_applications[application] = get_application_state(status, application)
            for unit in units:
                noncompliant_units[unit] = get_unit_state(status, unit)

    is_compliant = (len(noncompliant_applications) == 0) and (len(noncompliant_units) == 0)
    return is_compliant, JujuWaitState(
        message="waiting for unit removal",
        noncompliant_applications=noncompliant_applications,
        noncompliant_units=noncompliant_units,
    )
