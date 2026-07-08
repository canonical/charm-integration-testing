# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


from typing import Iterator

import jubilant
import yaml
from jubilant.statustypes import AppStatusRelation
from juju import (
    JujuApplicationState,
    JujuIntegrationApplication,
    JujuUnitAgentState,
    JujuUnitState,
    JujuWaitState,
)


def _parse_bundle(
    bundle_path: str,
) -> tuple[list[str], list[tuple[JujuIntegrationApplication, JujuIntegrationApplication]]]:
    with open(bundle_path) as f:
        # HACK(@motjuste): we may have multi-document yaml, just take first
        data = next(yaml.safe_load_all(f))

    app_names = list(data.get("applications", {}).keys())

    integrations = []
    for relation in data.get("relations", []):
        # Normalise nested lists: [["app1:ep1"], ["app2:ep2"]] → ["app1:ep1", "app2:ep2"]
        endpoints = [r[0] if isinstance(r, list) else r for r in relation]
        left_app, left_ep = endpoints[0].split(":", 1)
        right_app, right_ep = endpoints[1].split(":", 1)
        integrations.append(
            (JujuIntegrationApplication(left_app, left_ep), JujuIntegrationApplication(right_app, right_ep))
        )

    return app_names, integrations


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
    for offer, info in status.app_endpoints.items():
        for endpoint, integrated_apps in info.relations.items():
            for related_app in integrated_apps:
                yield (offer, endpoint, AppStatusRelation(related_app, info.endpoints[endpoint].interface))


def get_integrations(status: jubilant.Status) -> set[tuple[JujuIntegrationApplication, JujuIntegrationApplication]]:
    pairs: set[tuple[JujuIntegrationApplication, JujuIntegrationApplication]] = set()

    # Local app <-> local app integrations
    local_endpoint_integrations = list(generate_endpoint_integrations(status))
    for application_1, endpoint_1, integration_1 in local_endpoint_integrations:
        for application_2, endpoint_2, integration_2 in local_endpoint_integrations:
            if (
                integration_1.interface == integration_2.interface
                and application_1 == integration_2.related_app
                and application_2 == integration_1.related_app
            ):
                pairs.add(
                    (
                        JujuIntegrationApplication(application_1, endpoint_1),
                        JujuIntegrationApplication(application_2, endpoint_2),
                    )
                )

    # Local app <-> SAAS (remote app) integrations: the SAAS side is in status.app_endpoints
    for application, endpoint, integration in local_endpoint_integrations:
        if integration.related_app in status.app_endpoints:
            # Find which endpoint on the remote app corresponds to this interface
            remote = status.app_endpoints[integration.related_app]
            remote_endpoint = next(
                (ep_name for ep_name, ep in remote.endpoints.items() if ep.interface == integration.interface),
                None,
            )
            if remote_endpoint is not None:
                pairs.add(
                    (
                        JujuIntegrationApplication(application, endpoint),
                        JujuIntegrationApplication(integration.related_app, remote_endpoint),
                    )
                )

    return pairs


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
    is_k8s = status.model.type == "caas"

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
        invalid_units = []
        for unit, unit_info in status.get_units(application).items():
            if unit_info.juju_status.current in {"idle", "executing"}:
                valid_units.append(unit)
            else:
                noncompliant_units[unit] = get_unit_state(status, unit)
                invalid_units.append(unit)

        # If K8s, Compare with the target scale of the application
        #   machine charms always return scale = 0
        # If not k8s, application is non-compliant if any units are non-compliant
        if (is_k8s and application_info.scale != len(valid_units)) or (not is_k8s and len(invalid_units)):
            noncompliant_applications[application] = get_application_state(status, application)

    is_compliant = (len(noncompliant_applications) == 0) and (len(noncompliant_units) == 0)
    return is_compliant, JujuWaitState(
        message="waiting for application to scale",
        noncompliant_applications=noncompliant_applications,
        noncompliant_units=noncompliant_units,
    )


def application_is_on_revision(status: jubilant.Status, application: str, revision: int) -> tuple[bool, JujuWaitState]:
    noncompliant_applications: dict[str, JujuApplicationState | None] = {}

    if application not in status.apps:
        noncompliant_applications[application] = None
    elif status.apps[application].charm_rev != revision:
        noncompliant_applications[application] = get_application_state(status, application)

    return len(noncompliant_applications) == 0, JujuWaitState(
        message=f"waiting for application revision {revision}",
        noncompliant_applications=noncompliant_applications,
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
    existing_integrations = {frozenset(i) for i in get_integrations(status)}
    integrations = integrations_args if integrations_args else [tuple(i) for i in existing_integrations]

    noncompliant_applications: dict[str, JujuApplicationState | None] = {}
    for integration in integrations:
        if frozenset(integration) in existing_integrations:
            for endpoint in integration:
                # Skip SAAS applications (consumed offers) - they're not local apps in this model
                if endpoint.application in status.apps:
                    noncompliant_applications[endpoint.application] = get_application_state(
                        status, endpoint.application
                    )

    return len(noncompliant_applications) == 0, JujuWaitState(
        message="waiting for integration removal",
        noncompliant_applications=noncompliant_applications,
    )


def bundle_integrations_exist(
    status: jubilant.Status, *integrations_args: tuple[JujuIntegrationApplication, JujuIntegrationApplication]
) -> tuple[bool, JujuWaitState]:
    existing_integrations = {frozenset(i) for i in get_integrations(status)}

    noncompliant_applications: dict[str, JujuApplicationState | None] = {}
    for integration in integrations_args:
        if frozenset(integration) not in existing_integrations:
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


def bundle_applications_integrations_exist(status: jubilant.Status, bundle: str) -> tuple[bool, JujuWaitState]:
    app_names, integrations = _parse_bundle(bundle)

    if app_names:
        ok, state = all_statuses_are_in(status, *app_names)
        if not ok:
            return ok, state

    if integrations:
        ok, state = bundle_integrations_exist(status, *integrations)
        if not ok:
            return ok, state

    return True, JujuWaitState()
