# Copyright (C) 2025 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import z3

from .assertion_tags import (
    AppEndpointPayload,
    ApplicationExistsTag,
    ApplicationIntegrationAppsMapToCharmsTag,
    ApplicationIntegrationExistsTag,
    CharmEndpointNonOptionalTag,
    CharmEndpointPayload,
    CharmExistsFromApplicationTag,
    CharmExistsFromIntegrationTag,
    CharmIntegrationExistsFromApplicationIntegrationTag,
    CharmIntegrationMappedToSingleApplicationIntegrationTag,
    CharmMappedToSingleApplicationTag,
    EndpointCountMatchesIntegrationsTag,
    EndpointRespectsLimitTag,
)
from .problem_space import ProblemSpace, ProblemSpaceCharm


def _app_endpoints_from_integration(integration: frozenset[tuple[str, str]]) -> list[AppEndpointPayload]:
    return [AppEndpointPayload(application=app, endpoint=ep) for app, ep in sorted(integration)]


def _charm_endpoint_payload(charm: ProblemSpaceCharm, charm_id: int, endpoint: str | None) -> CharmEndpointPayload:
    return CharmEndpointPayload(charm_name=charm.spec.name, charm_id=charm_id, endpoint=endpoint)


def _charm_endpoints_from_integration(
    integration: frozenset[tuple[int, str]], problem_space: ProblemSpace
) -> list[CharmEndpointPayload]:
    return [
        _charm_endpoint_payload(problem_space.charms[charm_id], charm_id, endpoint_name)
        for charm_id, endpoint_name in sorted(integration)
    ]


def add_application_constraints(solver: z3.Solver, problem_space: ProblemSpace) -> None:
    for application in problem_space.application_constraints.keys():
        solver.assert_and_track(
            z3.Sum(
                [z3.If(m, 1, 0) for (a, c), m in problem_space.application_to_charm.items() if a == application]
                + [z3.IntVal(0)]
            )
            == 1,
            ApplicationExistsTag(application=application).encode(),
        )

    for charm_id, charm in enumerate(problem_space.charms):
        solver.assert_and_track(
            z3.Sum(
                [z3.If(m, 1, 0) for (a, c), m in problem_space.application_to_charm.items() if c == charm_id]
                + [z3.IntVal(0)]
            )
            <= 1,
            CharmMappedToSingleApplicationTag(charm=_charm_endpoint_payload(charm, charm_id, None)).encode(),
        )

    for (application, charm_id), mapping_var in problem_space.application_to_charm.items():
        charm_var = problem_space.charms[charm_id].exists
        solver.assert_and_track(
            z3.Implies(mapping_var, charm_var),
            CharmExistsFromApplicationTag(
                application=application,
                charm=_charm_endpoint_payload(problem_space.charms[charm_id], charm_id, None),
            ).encode(),
        )

    for application_integration in problem_space.integration_constraints:
        solver.assert_and_track(
            z3.Sum(
                [
                    z3.If(m, 1, 0)
                    for (a_ep, c_ep), m in problem_space.application_integration_to_charm_integration.items()
                    if a_ep == application_integration
                ]
                + [z3.IntVal(0)]
            )
            == 1,
            ApplicationIntegrationExistsTag(
                integration=_app_endpoints_from_integration(application_integration)
            ).encode(),
        )

    for charm_integration in problem_space.charm_integrations.keys():
        solver.assert_and_track(
            z3.Sum(
                [
                    z3.If(m, 1, 0)
                    for (a_ep, c_ep), m in problem_space.application_integration_to_charm_integration.items()
                    if c_ep == charm_integration
                ]
                + [z3.IntVal(0)]
            )
            <= 1,
            CharmIntegrationMappedToSingleApplicationIntegrationTag(
                charm_integration=_charm_endpoints_from_integration(charm_integration, problem_space)
            ).encode(),
        )

    for (
        application_integration,
        charm_integration,
    ), mapping_var in problem_space.application_integration_to_charm_integration.items():
        charm_integration_var = problem_space.charm_integrations[charm_integration].exists
        solver.assert_and_track(
            z3.Implies(mapping_var, charm_integration_var),
            CharmIntegrationExistsFromApplicationIntegrationTag(
                application_integration=_app_endpoints_from_integration(application_integration),
                charm_integration=_charm_endpoints_from_integration(charm_integration, problem_space),
            ).encode(),
        )

    for (
        application_integration,
        charm_integration,
    ), mapping_var in problem_space.application_integration_to_charm_integration.items():
        for app_name, app_ep in application_integration:
            for charm_id, charm_ep in charm_integration:
                if app_ep != charm_ep:
                    continue
                app_to_charm_var = problem_space.application_to_charm.get((app_name, charm_id))
                if app_to_charm_var is None:
                    continue
                solver.assert_and_track(
                    z3.Implies(mapping_var, app_to_charm_var),
                    ApplicationIntegrationAppsMapToCharmsTag(
                        application=app_name,
                        application_endpoint=app_ep,
                        charm=_charm_endpoint_payload(problem_space.charms[charm_id], charm_id, charm_ep),
                    ).encode(),
                )


def add_charm_constraints(solver: z3.Solver, problem_space: ProblemSpace) -> None:
    for integration_key, integration_var in problem_space.charm_integrations.items():
        charm_ids = [charm_id for (charm_id, _) in integration_key]
        for charm_id in charm_ids:
            charm_var = problem_space.charms[charm_id].exists
            solver.assert_and_track(
                z3.Implies(integration_var.exists, charm_var),
                CharmExistsFromIntegrationTag(
                    charm=_charm_endpoint_payload(problem_space.charms[charm_id], charm_id, None),
                    integration=_charm_endpoints_from_integration(integration_key, problem_space),
                ).encode(),
            )

    for charm_id, charm in enumerate(problem_space.charms):
        for endpoint_name, endpoint in charm.endpoints.items():
            integrations_using_endpoint = []
            for integration_key, integration_var in problem_space.charm_integrations.items():
                if (charm_id, endpoint_name) in integration_key:
                    integrations_using_endpoint.append(integration_var.exists)
            usage_count = len(integrations_using_endpoint)
            solver.assert_and_track(
                endpoint.count == z3.Sum([z3.If(i, 1, 0) for i in integrations_using_endpoint] + [z3.IntVal(0)]),
                EndpointCountMatchesIntegrationsTag(
                    charm=_charm_endpoint_payload(charm, charm_id, endpoint_name),
                    usage_count=usage_count,
                ).encode(),
            )

    for charm_id, charm in enumerate(problem_space.charms):
        for endpoint_name, endpoint in charm.spec.endpoints.items():
            if not endpoint.optional:
                solver.assert_and_track(
                    z3.Implies(charm.exists, charm.endpoints[endpoint_name].count >= 1),
                    CharmEndpointNonOptionalTag(charm=_charm_endpoint_payload(charm, charm_id, endpoint_name)).encode(),
                )

    for charm_id, charm in enumerate(problem_space.charms):
        for endpoint_name, endpoint in charm.spec.endpoints.items():
            if endpoint.limit is not None:
                solver.assert_and_track(
                    z3.Implies(charm.exists, charm.endpoints[endpoint_name].count <= endpoint.limit),
                    EndpointRespectsLimitTag(
                        charm=_charm_endpoint_payload(charm, charm_id, endpoint_name),
                        limit=endpoint.limit,
                    ).encode(),
                )


def add_constraints(solver: z3.Solver, problem_space: ProblemSpace) -> None:
    add_application_constraints(solver, problem_space)
    add_charm_constraints(solver, problem_space)
