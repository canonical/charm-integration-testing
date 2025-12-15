# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


from typing import Iterator

import jubilant
from jubilant.statustypes import AppStatusRelation
from juju import JujuApplicationState, JujuIntegration, JujuIntegrationApplication, JujuUnitState, JujuWaitState


def get_unit_info(status: jubilant.Status, unit: str) -> jubilant.statustypes.UnitStatus | None:
    application, unit_id = unit.split("/")
    for possible_unit, unit_info in status.get_units(application).items():
        if possible_unit == unit:
            return unit_info
        if unit_id == "leader" and unit_info.leader:
            return unit_info
    return None


def generate_endpoint_integrations(status: jubilant.Status) -> Iterator[tuple[str, str, AppStatusRelation]]:
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
        status=unit_info.workload_status.current if unit_info is not None else "",
        message=unit_info.workload_status.message if unit_info is not None else "",
    )


def all_statuses_are_in(
    expected: set[str], status: jubilant.Status, *application_args: str
) -> tuple[bool, JujuWaitState]:
    applications = set(application_args if application_args else status.apps.keys())

    noncompliant_applications: dict[str, None | JujuApplicationState] = {}
    noncompliant_units: dict[str, JujuUnitState | None] = {}
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


def applications_are_scaled(status: jubilant.Status, *application_args: str) -> tuple[bool, JujuWaitState]:
    # Check applications have reached desired scale
    # See https://github.com/juju/juju/blob/add3443726e40faebaba0103289c6660251fa1eb/cmd/juju/status/formatted.go#L239
    applications = set(application_args if application_args else status.apps.keys())

    noncompliant_applications: dict[str, JujuApplicationState | None] = {}
    noncompliant_units: dict[str, JujuUnitState | None] = {}
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


def units_have_message(message: str, status: jubilant.Status, *unit_args: str) -> tuple[bool, JujuWaitState]:
    units = set(
        (unit for application in status.apps for unit in status.get_units(application)) if not unit_args else unit_args
    )

    noncompliant_units: dict[str, JujuUnitState | None] = {}
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


def applications_are_removed(status: jubilant.Status, *application_args: str) -> tuple[bool, JujuWaitState]:
    applications = set(application_args if application_args else status.apps.keys())

    noncompliant_applications: dict[str, JujuApplicationState | None] = {}
    for application in applications:
        if application in status.apps:
            noncompliant_applications[application] = get_application_state(status, application)

    return len(noncompliant_applications) == 0, JujuWaitState(
        message="waiting for application removal",
        noncompliant_applications=noncompliant_applications,
    )


def integrations_are_removed(
    status: jubilant.Status, *integrations_args: tuple[JujuIntegrationApplication, JujuIntegrationApplication]
) -> tuple[bool, JujuWaitState]:
    existing_integrations = {integration.applications for integration in get_integrations(status)}
    integrations = integrations_args if integrations_args else existing_integrations

    noncompliant_applications: dict[str, JujuApplicationState | None] = {}
    for integration in integrations:
        if set(integration) in existing_integrations:
            for endpoint in integration:
                noncompliant_applications[endpoint.application] = get_application_state(status, endpoint.application)

    return len(noncompliant_applications) == 0, JujuWaitState(
        message="waiting for integration removal",
        noncompliant_applications=noncompliant_applications,
    )


def applications_have_no_units(status: jubilant.Status, *application_args: str) -> tuple[bool, JujuWaitState]:
    applications = set(application_args if application_args else status.apps.keys())

    noncompliant_applications: dict[str, JujuApplicationState | None] = {}
    noncompliant_units: dict[str, JujuUnitState | None] = {}
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
