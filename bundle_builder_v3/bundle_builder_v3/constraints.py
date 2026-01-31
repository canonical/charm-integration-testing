# Copyright (C) 2026 Canonical Ltd

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
    CharmCustomConstraintTag,
    CharmDependencyAcyclicTag,
    CharmEndpointNonOptionalTag,
    CharmEndpointPayload,
    CharmExistsFromApplicationTag,
    CharmExistsFromIntegrationTag,
    CharmIntegrationExistsFromApplicationIntegrationTag,
    CharmIntegrationMappedToSingleApplicationIntegrationTag,
    CharmMappedToSingleApplicationTag,
    CharmPayload,
    EndpointCountMatchesIntegrationsTag,
    EndpointRespectsLimitTag,
)
from .domain import ApplicationIntegration, CharmIntegration, Domain, DomainCharm


def _app_endpoints_from_integration(integration: ApplicationIntegration) -> list[AppEndpointPayload]:
    return [
        AppEndpointPayload(application=integration.endpoint1.application, endpoint=integration.endpoint1.endpoint),
        AppEndpointPayload(application=integration.endpoint2.application, endpoint=integration.endpoint2.endpoint),
    ]


def _charm_payload(charm: DomainCharm, charm_id: int) -> CharmPayload:
    return CharmPayload(charm_name=charm.spec.name, charm_id=charm_id)


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
    # Ensure each application maps to exactly one charm
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

    # Ensure each charm maps to at most one application
    for charm_id, charm in enumerate(domain.charms):
        solver.assert_and_track(
            z3.Sum(
                [z3.If(m, 1, 0) for mapping, m in domain.application_to_charm.items() if mapping.charm_id == charm_id]
                + [z3.IntVal(0)]
            )
            <= 1,
            CharmMappedToSingleApplicationTag(charm=_charm_payload(charm, charm_id)).encode(),
        )

    # Ensure charm exists if application-to-charm mapping is active
    for mapping, mapping_var in domain.application_to_charm.items():
        charm_var = domain.charms[mapping.charm_id].exists
        solver.assert_and_track(
            z3.Implies(mapping_var, charm_var),
            CharmExistsFromApplicationTag(
                application=mapping.application,
                charm=_charm_payload(domain.charms[mapping.charm_id], mapping.charm_id),
            ).encode(),
        )

    # Ensure each user-specified application integration maps to exactly one charm integration
    for app_integration in domain.integration_constraints:
        solver.assert_and_track(
            z3.Sum(
                [
                    z3.If(m, 1, 0)
                    for (a_int, c_int), m in domain.application_integration_to_charm_integration.items()
                    if a_int == app_integration
                ]
                + [z3.IntVal(0)]
            )
            == 1,
            ApplicationIntegrationExistsTag(integration=_app_endpoints_from_integration(app_integration)).encode(),
        )

    # Ensure each charm integration maps to at most one application integration
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

    # Ensure charm integration exists if application-to-charm integration mapping is active
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

    # Ensure application-to-charm mappings are active when integration mapping is active
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
    # Ensure both charms exist if integration exists
    for charm_integration, integration_var in domain.charm_integrations.items():
        charm_ids = [charm_integration.requires_endpoint.charm_id, charm_integration.provides_endpoint.charm_id]
        for charm_id in charm_ids:
            charm_var = domain.charms[charm_id].exists
            solver.assert_and_track(
                z3.Implies(integration_var.exists, charm_var),
                CharmExistsFromIntegrationTag(
                    charm=_charm_payload(domain.charms[charm_id], charm_id),
                    integration=_charm_endpoints_from_integration(charm_integration, domain),
                ).encode(),
            )

    # Ensure endpoint count equals number of integrations using that endpoint
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
            # Link integrated boolean to count
            solver.add(endpoint.integrated == (endpoint.count >= 1))


def add_charm_config_constraints(solver: z3.Solver, domain: Domain) -> None:
    # Ensure non-optional endpoints have at least one integration if charm exists
    for charm_id, charm in enumerate(domain.charms):
        for endpoint_name, spec_endpoint in charm.spec.endpoints.items():
            if not spec_endpoint.optional:
                solver.assert_and_track(
                    z3.Implies(charm.exists, charm.endpoints[endpoint_name].count >= 1),
                    CharmEndpointNonOptionalTag(charm=_charm_endpoint_payload(charm, charm_id, endpoint_name)).encode(),
                )

    # Ensure endpoint count respects limit if charm exists
    for charm_id, charm in enumerate(domain.charms):
        for endpoint_name, spec_endpoint in charm.spec.endpoints.items():
            if spec_endpoint.limit is not None:
                solver.assert_and_track(
                    z3.Implies(charm.exists, charm.endpoints[endpoint_name].count <= spec_endpoint.limit),
                    EndpointRespectsLimitTag(
                        charm=_charm_endpoint_payload(charm, charm_id, endpoint_name),
                        limit=spec_endpoint.limit,
                    ).encode(),
                )

    # Add config constraints
    for charm_id, charm in enumerate(domain.charms):
        # Config index must be in valid range if charm exists
        solver.add(
            z3.Implies(
                charm.exists,
                z3.And(
                    charm.config_index >= 0,
                    charm.config_index < len(charm.spec.configs),
                ),
            )
        )

        # Link config_index to config values
        for i, config in enumerate(charm.spec.configs):
            config_num = i
            for key, option in config.items():
                if option.value is None:
                    continue  # Skip unspecified values

                var = charm.config_vars[key]
                # Add constraint linking config_index to value
                if isinstance(option.value, str):
                    solver.add(z3.Implies(charm.config_index == config_num, var == z3.StringVal(option.value)))
                elif isinstance(option.value, bool):
                    solver.add(z3.Implies(charm.config_index == config_num, var == option.value))
                elif isinstance(option.value, int):
                    solver.add(z3.Implies(charm.config_index == config_num, var == option.value))
                elif isinstance(option.value, float):
                    solver.add(z3.Implies(charm.config_index == config_num, var == z3.RealVal(option.value)))

    # Add custom constraints from override files
    for charm_id, charm in enumerate(domain.charms):
        if not charm.spec.constraints:
            continue

        # Build declaration mapping: variable names to actual Z3 variables
        decls = {
            f"{endpoint_name}_count": endpoint_var.count for endpoint_name, endpoint_var in charm.endpoints.items()
        }
        decls.update(
            {endpoint_name: endpoint_var.integrated for endpoint_name, endpoint_var in charm.endpoints.items()}
        )
        # Add config variables (with config_ prefix for SMT-Lib)
        decls.update({f"config_{key}": var for key, var in charm.config_vars.items()})

        # Parse SMT-Lib with existing variables
        constraints = z3.parse_smt2_string(charm.spec.constraints, decls=decls)

        # Guard each constraint with "if charm exists"
        for idx, constraint in enumerate(constraints):
            solver.assert_and_track(
                z3.Implies(charm.exists, constraint),
                CharmCustomConstraintTag(charm=_charm_payload(charm, charm_id), assertion_idx=idx).encode(),
            )


def add_charm_dependency_constraints(solver: z3.Solver, domain: Domain) -> None:
    # Create rank variables for topological ordering to prevent cycles
    charm_count = len(domain.charms)
    ranks: list[z3.ArithRef] = [z3.Int(f"charm_{idx}_rank") for idx in range(charm_count)]

    # Bound each rank to [0, charm_count]
    for rank_var in ranks:
        solver.add(rank_var >= 0)
        solver.add(rank_var <= charm_count)

    # Enforce acyclic dependencies: requiring charm must have higher rank than providing charm
    # Skip if either endpoint is marked as acyclic (allows intentional cycles)
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
    add_charm_config_constraints(solver, domain)
    add_charm_dependency_constraints(solver, domain)
