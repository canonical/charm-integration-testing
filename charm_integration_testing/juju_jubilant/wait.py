# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


from typing import Iterator

import jubilant
from jubilant.statustypes import AppStatusRelation
from juju import (
    JujuApplicationState,
    JujuIntegrationApplication,
    JujuUnitAgentState,
    JujuUnitState,
    JujuWaitState,
)


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


def get_integrations(status: jubilant.Status) -> set[tuple[JujuIntegrationApplication, JujuIntegrationApplication]]:
    return {
        (JujuIntegrationApplication(application_1, endpoint_1), JujuIntegrationApplication(application_2, endpoint_2))
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


def get_unit_state(status: jubilant.Status, unit: str) -> JujuUnitState | None:
    application, _ = unit.split("/")
    application_info = status.apps[application]
    unit_info = get_unit_info(status, unit)

    if unit_info is None:
        return None
    else:
        return JujuUnitState(
            charm=application_info.charm,
            revision=application_info.charm_rev,
            status=unit_info.workload_status.current,
            message=unit_info.workload_status.message,
        )


def get_unit_agent_state(status: jubilant.Status, unit: str) -> JujuUnitAgentState:
    application, _ = unit.split("/")
    application_info = status.apps[application]
    unit_info = get_unit_info(status, unit)
    if unit_info is None:
        raise ValueError(f"Unit {unit} not found in status")
    return JujuUnitAgentState(
        charm=application_info.charm,
        revision=application_info.charm_rev,
        status=unit_info.juju_status.current,
        message=unit_info.juju_status.message,
    )


def all_statuses_are_in(
    status: jubilant.Status,
    *application_args: str,
    application_statuses: set[str] | None = None,
    unit_statuses: set[str] | None = None,
    unit_agent_statuses: set[str] | None = None,
) -> tuple[bool, JujuWaitState]:
    applications = set(application_args if application_args else status.apps.keys())

    noncompliant_applications: dict[str, JujuApplicationState | None] = {}
    noncompliant_units: dict[str, JujuUnitState | None] = {}
    noncompliant_unit_agents: dict[str, JujuUnitAgentState | None] = {}

    for application in applications:
        if application not in status.apps:
            noncompliant_applications[application] = None
            continue
        application_info = status.apps[application]

        # Check application status if specified
        if application_statuses is not None and application_info.app_status.current not in application_statuses:
            noncompliant_applications[application] = get_application_state(status, application)

        # Check unit and unit agent statuses
        for unit, unit_info in status.get_units(application).items():
            if unit_statuses is not None and unit_info.workload_status.current not in unit_statuses:
                noncompliant_units[unit] = get_unit_state(status, unit)
            if unit_agent_statuses is not None and unit_info.juju_status.current not in unit_agent_statuses:
                noncompliant_unit_agents[unit] = get_unit_agent_state(status, unit)

    is_compliant = (
        len(noncompliant_applications) == 0 and len(noncompliant_units) == 0 and len(noncompliant_unit_agents) == 0
    )

    status_parts = []
    if application_statuses is not None:
        status_parts.append(f"applications: [{', '.join(sorted(application_statuses))}]")
    if unit_statuses is not None:
        status_parts.append(f"units: [{', '.join(sorted(unit_statuses))}]")
    if unit_agent_statuses is not None:
        status_parts.append(f"unit agents: [{', '.join(sorted(unit_agent_statuses))}]")
    message = f"waiting for {', '.join(status_parts)}" if status_parts else "waiting"

    return is_compliant, JujuWaitState(
        message=message,
        noncompliant_applications=noncompliant_applications,
        noncompliant_units=noncompliant_units,
        noncompliant_unit_agents=noncompliant_unit_agents,
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
    if unit_args:
        units = set(unit_args)
    else:  # default to all units when no unit is passed
        units = set((unit for application in status.apps for unit in status.get_units(application)))

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
    if application_args:
        applications = set(application_args)
    else:  # default to all applications when no application is passed
        applications = set(status.apps.keys())

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
    existing_integrations = get_integrations(status)
    integrations = integrations_args if integrations_args else existing_integrations

    noncompliant_applications: dict[str, JujuApplicationState | None] = {}
    for integration in integrations:
        if integration in existing_integrations:
            for endpoint in integration:
                noncompliant_applications[endpoint.application] = get_application_state(status, endpoint.application)

    return len(noncompliant_applications) == 0, JujuWaitState(
        message="waiting for integration removal",
        noncompliant_applications=noncompliant_applications,
    )


def bundle_integrations_exist(
    status: jubilant.Status, *integrations_args: tuple[JujuIntegrationApplication, JujuIntegrationApplication]
) -> tuple[bool, JujuWaitState]:
    existing_integrations = get_integrations(status)

    noncompliant_applications: dict[str, JujuApplicationState | None] = {}
    for integration in integrations_args:
        if integration not in existing_integrations:
            for endpoint in integration:
                noncompliant_applications[endpoint.application] = (
                    get_application_state(status, endpoint.application) if endpoint.application in status.apps else None
                )

    return len(noncompliant_applications) == 0, JujuWaitState(
        message="waiting for bundle integrations",
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
