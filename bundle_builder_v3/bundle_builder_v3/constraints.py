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
from .domain import ApplicationIntegration, CharmIntegration, Domain, DomainCharm


def _app_endpoints_from_integration(integration: ApplicationIntegration) -> list[AppEndpointPayload]:
    return [
        AppEndpointPayload(application=integration.endpoint1.application, endpoint=integration.endpoint1.endpoint),
        AppEndpointPayload(application=integration.endpoint2.application, endpoint=integration.endpoint2.endpoint),
    ]


def _charm_endpoint_payload(charm: DomainCharm, charm_id: int, endpoint: str | None) -> CharmEndpointPayload:
    return CharmEndpointPayload(charm_name=charm.spec.name, charm_id=charm_id, endpoint=endpoint)


def _charm_endpoints_from_integration(integration: CharmIntegration, domain: Domain) -> list[CharmEndpointPayload]:
    return [
        _charm_endpoint_payload(
            domain.charms[integration.requires_endpoint.charm_id],
            integration.requires_endpoint.charm_id,
            integration.requires_endpoint.endpoint,
        ),
        _charm_endpoint_payload(
            domain.charms[integration.provides_endpoint.charm_id],
            integration.provides_endpoint.charm_id,
            integration.provides_endpoint.endpoint,
        ),
    ]


def add_application_constraints(solver: z3.Solver, domain: Domain) -> None:
    for application in domain.application_constraints.keys():
        solver.assert_and_track(
            z3.Sum(
                [
                    z3.If(m, 1, 0)
                    for mapping, m in domain.application_to_charm.items()
                    if mapping.application == application
                ]
                + [z3.IntVal(0)]
            )
            == 1,
            ApplicationExistsTag(application=application).encode(),
        )

    for charm_id, charm in enumerate(domain.charms):
        solver.assert_and_track(
            z3.Sum(
                [z3.If(m, 1, 0) for mapping, m in domain.application_to_charm.items() if mapping.charm_id == charm_id]
                + [z3.IntVal(0)]
            )
            <= 1,
            CharmMappedToSingleApplicationTag(charm=_charm_endpoint_payload(charm, charm_id, None)).encode(),
        )

    for mapping, mapping_var in domain.application_to_charm.items():
        charm_var = domain.charms[mapping.charm_id].exists
        solver.assert_and_track(
            z3.Implies(mapping_var, charm_var),
            CharmExistsFromApplicationTag(
                application=mapping.application,
                charm=_charm_endpoint_payload(domain.charms[mapping.charm_id], mapping.charm_id, None),
            ).encode(),
        )

    for app_integration in domain.integration_constraints:
        relevant_mappings = [
            z3.If(m, 1, 0)
            for (a_int, c_int), m in domain.application_integration_to_charm_integration.items()
            if a_int == app_integration
        ]
        # Only add constraint if mappings exist; otherwise let application_exists fail
        if len(relevant_mappings) > 0:
            solver.assert_and_track(
                z3.Sum(relevant_mappings + [z3.IntVal(0)]) == 1,
                ApplicationIntegrationExistsTag(integration=_app_endpoints_from_integration(app_integration)).encode(),
            )

    for charm_integration in domain.charm_integrations.keys():
        solver.assert_and_track(
            z3.Sum(
                [
                    z3.If(m, 1, 0)
                    for (a_int, c_int), m in domain.application_integration_to_charm_integration.items()
                    if c_int == charm_integration
                ]
                + [z3.IntVal(0)]
            )
            <= 1,
            CharmIntegrationMappedToSingleApplicationIntegrationTag(
                charm_integration=_charm_endpoints_from_integration(charm_integration, domain)
            ).encode(),
        )

    for (
        app_integration,
        charm_integration,
    ), mapping_var in domain.application_integration_to_charm_integration.items():
        charm_integration_var = domain.charm_integrations[charm_integration].exists
        solver.assert_and_track(
            z3.Implies(mapping_var, charm_integration_var),
            CharmIntegrationExistsFromApplicationIntegrationTag(
                application_integration=_app_endpoints_from_integration(app_integration),
                charm_integration=_charm_endpoints_from_integration(charm_integration, domain),
            ).encode(),
        )

    # Ensure that if an integration mapping is active, the corresponding application-to-charm mappings are active
    for (
        app_integration,
        charm_integration,
    ), mapping_var in domain.application_integration_to_charm_integration.items():
        # ApplicationIntegration is unordered, CharmIntegration is ordered
        # Figure out which endpoint in app_integration corresponds to requires/provides in charm_integration
        charm_req = charm_integration.requires_endpoint
        charm_prov = charm_integration.provides_endpoint

        # Try both orderings to find which matches
        if (
            app_integration.endpoint1.endpoint == charm_req.endpoint
            and app_integration.endpoint2.endpoint == charm_prov.endpoint
        ):
            # endpoint1 = requires, endpoint2 = provides
            app_req = app_integration.endpoint1
            app_prov = app_integration.endpoint2
        elif (
            app_integration.endpoint1.endpoint == charm_prov.endpoint
            and app_integration.endpoint2.endpoint == charm_req.endpoint
        ):
            # endpoint1 = provides, endpoint2 = requires
            app_req = app_integration.endpoint2
            app_prov = app_integration.endpoint1
        else:
            raise ValueError(
                f"Integration mapping exists but endpoint names don't match: "
                f"{app_integration} -> {charm_integration}"
            )

        # Find the mappings we need
        req_mapping_key = next(
            (
                m
                for m in domain.application_to_charm.keys()
                if m.application == app_req.application and m.charm_id == charm_req.charm_id
            ),
            None,
        )
        prov_mapping_key = next(
            (
                m
                for m in domain.application_to_charm.keys()
                if m.application == app_prov.application and m.charm_id == charm_prov.charm_id
            ),
            None,
        )

        if req_mapping_key is None or prov_mapping_key is None:
            raise ValueError(
                f"Integration mapping exists but application-to-charm mappings don't exist: "
                f"{app_integration} -> {charm_integration}"
            )

        req_mapping = domain.application_to_charm[req_mapping_key]
        prov_mapping = domain.application_to_charm[prov_mapping_key]

        solver.assert_and_track(
            z3.Implies(
                mapping_var,
                z3.And(req_mapping, prov_mapping),
            ),
            ApplicationIntegrationAppsMapToCharmsTag(
                application=app_req.application,
                application_endpoint=app_req.endpoint,
                application_integration=_app_endpoints_from_integration(app_integration),
                charm_integration=_charm_endpoints_from_integration(charm_integration, domain),
                charm=_charm_endpoint_payload(
                    domain.charms[charm_req.charm_id], charm_req.charm_id, charm_req.endpoint
                ),
            ).encode(),
        )


def add_charm_constraints(solver: z3.Solver, domain: Domain) -> None:
    for charm_integration, integration_var in domain.charm_integrations.items():
        charm_ids = [charm_integration.requires_endpoint.charm_id, charm_integration.provides_endpoint.charm_id]
        for charm_id in charm_ids:
            charm_var = domain.charms[charm_id].exists
            solver.assert_and_track(
                z3.Implies(integration_var.exists, charm_var),
                CharmExistsFromIntegrationTag(
                    charm=_charm_endpoint_payload(domain.charms[charm_id], charm_id, None),
                    integration=_charm_endpoints_from_integration(charm_integration, domain),
                ).encode(),
            )

    for charm_id, charm in enumerate(domain.charms):
        for endpoint_name, endpoint in charm.endpoints.items():
            integrations_using_endpoint = []
            for charm_integration, integration_var in domain.charm_integrations.items():
                # Check if this charm/endpoint is in the integration
                if (
                    charm_integration.requires_endpoint.charm_id == charm_id
                    and charm_integration.requires_endpoint.endpoint == endpoint_name
                ) or (
                    charm_integration.provides_endpoint.charm_id == charm_id
                    and charm_integration.provides_endpoint.endpoint == endpoint_name
                ):
                    integrations_using_endpoint.append(integration_var.exists)
            usage_count = len(integrations_using_endpoint)
            solver.assert_and_track(
                endpoint.count == z3.Sum([z3.If(i, 1, 0) for i in integrations_using_endpoint] + [z3.IntVal(0)]),
                EndpointCountMatchesIntegrationsTag(
                    charm=_charm_endpoint_payload(charm, charm_id, endpoint_name),
                    usage_count=usage_count,
                ).encode(),
            )

    for charm_id, charm in enumerate(domain.charms):
        for endpoint_name, endpoint in charm.spec.endpoints.items():
            if not endpoint.optional:
                solver.assert_and_track(
                    z3.Implies(charm.exists, charm.endpoints[endpoint_name].count >= 1),
                    CharmEndpointNonOptionalTag(charm=_charm_endpoint_payload(charm, charm_id, endpoint_name)).encode(),
                )

    for charm_id, charm in enumerate(domain.charms):
        for endpoint_name, endpoint in charm.spec.endpoints.items():
            if endpoint.limit is not None:
                solver.assert_and_track(
                    z3.Implies(charm.exists, charm.endpoints[endpoint_name].count <= endpoint.limit),
                    EndpointRespectsLimitTag(
                        charm=_charm_endpoint_payload(charm, charm_id, endpoint_name),
                        limit=endpoint.limit,
                    ).encode(),
                )


def add_charm_dependency_constraints(solver: z3.Solver, domain: Domain) -> None:
    # Create an integer rank variable for each charm to establish topological ordering
    charm_count = len(domain.charms)
    ranks: list[z3.ArithRef] = [z3.Int(f"charm_{idx}_rank") for idx in range(charm_count)]

    # Bound each rank to [0, charm_count]
    for rank_var in ranks:
        solver.add(rank_var >= 0)
        solver.add(rank_var <= charm_count)

    # For each integration, enforce that requiring charm has higher rank than providing charm
    for charm_integration, integration_var in domain.charm_integrations.items():
        # With semantic ordering, we can directly access requires and provides endpoints
        charm_req = charm_integration.requires_endpoint
        charm_prov = charm_integration.provides_endpoint

        # Look up endpoint specifications
        requires_spec = domain.charms[charm_req.charm_id].spec.endpoints[charm_req.endpoint]
        provides_spec = domain.charms[charm_prov.charm_id].spec.endpoints[charm_prov.endpoint]

        # Skip rank constraint if either endpoint is marked as acyclic (allows cycles)
        if requires_spec.acyclic or provides_spec.acyclic:
            continue

        # Assert: if integration exists, requiring charm must have higher rank than providing charm
        solver.assert_and_track(
            z3.Implies(integration_var.exists, ranks[charm_req.charm_id] > ranks[charm_prov.charm_id]),
            CharmDependencyAcyclicTag(
                requiring_charm=_charm_endpoint_payload(
                    domain.charms[charm_req.charm_id], charm_req.charm_id, charm_req.endpoint
                ),
                providing_charm=_charm_endpoint_payload(
                    domain.charms[charm_prov.charm_id], charm_prov.charm_id, charm_prov.endpoint
                ),
            ).encode(),
        )


def add_constraints(solver: z3.Solver, domain: Domain) -> None:
    add_application_constraints(solver, domain)
    add_charm_constraints(solver, domain)
    add_charm_dependency_constraints(solver, domain)
