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
    CharmDependencyAcyclicTag,
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
from .charm import EndpointType
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
        relevant_mappings = [
            z3.If(m, 1, 0)
            for (a_ep, c_ep), m in problem_space.application_integration_to_charm_integration.items()
            if a_ep == application_integration
        ]
        # Only add constraint if mappings exist; otherwise let application_exists fail
        if len(relevant_mappings) > 0:
            solver.assert_and_track(
                z3.Sum(relevant_mappings + [z3.IntVal(0)]) == 1,
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
    
    # Ensure that if an integration mapping is active, the corresponding application-to-charm mappings are active
    for (
        application_integration,
        charm_integration,
    ), mapping_var in problem_space.application_integration_to_charm_integration.items():
        # application_integration: frozenset of (app_name, endpoint_name) pairs
        # charm_integration: frozenset of (charm_id, endpoint_name) pairs
        app_ep_1, app_ep_2 = sorted(application_integration)
        charm_ep_1, charm_ep_2 = sorted(charm_integration)
        
        # Check both possible orientations and add constraints for valid ones
        # Option A: app1 maps to charm1 AND app2 maps to charm2
        if (
            app_ep_1[1] == charm_ep_1[1]
            and app_ep_2[1] == charm_ep_2[1]
            and (app_ep_1[0], charm_ep_1[0]) in problem_space.application_to_charm
            and (app_ep_2[0], charm_ep_2[0]) in problem_space.application_to_charm
        ):
            solver.assert_and_track(
                z3.Implies(
                    mapping_var,
                    z3.And(
                        problem_space.application_to_charm[(app_ep_1[0], charm_ep_1[0])],
                        problem_space.application_to_charm[(app_ep_2[0], charm_ep_2[0])],
                    ),
                ),
                ApplicationIntegrationAppsMapToCharmsTag(
                    application=app_ep_1[0],
                    application_endpoint=app_ep_1[1],
                    application_integration=_app_endpoints_from_integration(application_integration),
                    charm_integration=_charm_endpoints_from_integration(charm_integration, problem_space),
                    charm=_charm_endpoint_payload(problem_space.charms[charm_ep_1[0]], charm_ep_1[0], charm_ep_1[1]),
                ).encode(),
            )
        # Option B: app1 maps to charm2 AND app2 maps to charm1
        elif (
            app_ep_1[1] == charm_ep_2[1]
            and app_ep_2[1] == charm_ep_1[1]
            and (app_ep_1[0], charm_ep_2[0]) in problem_space.application_to_charm
            and (app_ep_2[0], charm_ep_1[0]) in problem_space.application_to_charm
        ):
            solver.assert_and_track(
                z3.Implies(
                    mapping_var,
                    z3.And(
                        problem_space.application_to_charm[(app_ep_1[0], charm_ep_2[0])],
                        problem_space.application_to_charm[(app_ep_2[0], charm_ep_1[0])],
                    ),
                ),
                ApplicationIntegrationAppsMapToCharmsTag(
                    application=app_ep_1[0],
                    application_endpoint=app_ep_1[1],
                    application_integration=_app_endpoints_from_integration(application_integration),
                    charm_integration=_charm_endpoints_from_integration(charm_integration, problem_space),
                    charm=_charm_endpoint_payload(problem_space.charms[charm_ep_2[0]], charm_ep_2[0], charm_ep_2[1]),
                ).encode(),
            )
        else:
            # This should never happen due to problem_space filtering
            raise ValueError(
                f"Integration mapping exists but neither orientation is valid: "
                f"{application_integration} -> {charm_integration}"
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


def add_charm_dependency_constraints(solver: z3.Solver, problem_space: ProblemSpace) -> None:
    # Create an integer rank variable for each charm to establish topological ordering
    charm_count = len(problem_space.charms)
    ranks: list[z3.ArithRef] = [z3.Int(f"charm_{idx}_rank") for idx in range(charm_count)]

    # Bound each rank to [0, charm_count]
    for rank_var in ranks:
        solver.add(rank_var >= 0)
        solver.add(rank_var <= charm_count)

    # For each integration, enforce that requiring charm has higher rank than providing charm
    for integration_key, integration_var in problem_space.charm_integrations.items():
        # Unpack the two endpoints in the integration
        (charm_id_1, endpoint_name_1), (charm_id_2, endpoint_name_2) = integration_key
        
        # Look up endpoint specifications
        charm_spec_1 = problem_space.charms[charm_id_1].spec
        endpoint_spec_1 = charm_spec_1.endpoints[endpoint_name_1]
        charm_spec_2 = problem_space.charms[charm_id_2].spec
        endpoint_spec_2 = charm_spec_2.endpoints[endpoint_name_2]

        # Determine which endpoint is REQUIRES and which is PROVIDES
        if endpoint_spec_1.type == EndpointType.REQUIRES:
            requires_id, requires_ep_name, requires_spec = charm_id_1, endpoint_name_1, endpoint_spec_1
            provides_id, provides_ep_name, provides_spec = charm_id_2, endpoint_name_2, endpoint_spec_2
        else:
            requires_id, requires_ep_name, requires_spec = charm_id_2, endpoint_name_2, endpoint_spec_2
            provides_id, provides_ep_name, provides_spec = charm_id_1, endpoint_name_1, endpoint_spec_1

        # Skip rank constraint if either endpoint is marked as acyclic (allows cycles)
        if requires_spec.acyclic or provides_spec.acyclic:
            continue

        # Assert: if integration exists, requiring charm must have higher rank than providing charm
        solver.assert_and_track(
            z3.Implies(integration_var.exists, ranks[requires_id] > ranks[provides_id]),
            CharmDependencyAcyclicTag(
                requiring_charm=_charm_endpoint_payload(
                    problem_space.charms[requires_id], requires_id, requires_ep_name
                ),
                providing_charm=_charm_endpoint_payload(
                    problem_space.charms[provides_id], provides_id, provides_ep_name
                ),
            ).encode(),
        )


def add_constraints(solver: z3.Solver, problem_space: ProblemSpace) -> None:
    add_application_constraints(solver, problem_space)
    add_charm_constraints(solver, problem_space)
    add_charm_dependency_constraints(solver, problem_space)
