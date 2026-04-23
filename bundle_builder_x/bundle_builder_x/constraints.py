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

import z3  # type: ignore[import-untyped]

from .assertion_tags import (
    AppEndpointPayload,
    ApplicationExistsTag,
    ApplicationIntegrationAppsMapToCharmsTag,
    ApplicationIntegrationExistsTag,
    CharmCustomConstraintTag,
    CharmDependencyCyclicTag,
    CharmEndpointNonOptionalTag,
    CharmEndpointPayload,
    CharmExistsFromApplicationTag,
    CharmExistsFromIntegrationTag,
    CharmIntegrationExistsFromApplicationIntegrationTag,
    CharmIntegrationMappedToSingleApplicationIntegrationTag,
    CharmMappedToSingleApplicationTag,
    CharmPayload,
    CharmRankBoundedTag,
    EndpointCountMatchesIntegrationsTag,
    EndpointIntegratedMatchesCountTag,
    EndpointRespectsLimitTag,
)
from .domain import (
    ApplicationIntegration,
    ApplicationToCharmMapping,
    CharmIntegration,
    Domain,
    DomainCharm,
)
from .dsl_lowering import DSLLoweringError, LoweringContext, config_value_to_z3, lower


def _app_endpoints_from_integration(integration: ApplicationIntegration) -> list[AppEndpointPayload]:
    return [
        AppEndpointPayload(application=integration.endpoint_1.application, endpoint=integration.endpoint_1.endpoint),
        AppEndpointPayload(application=integration.endpoint_2.application, endpoint=integration.endpoint_2.endpoint),
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
    # Snapshot aggregated mappings once to avoid rebuilding dicts in loops.
    app_to_charm = {k: v for mc in domain.models.values() for k, v in mc.application_to_charm.items()}
    app_int_to_charm_int = {
        k: v for mc in domain.models.values() for k, v in mc.application_integration_to_charm_integration.items()
    }

    # Ensure each application maps to exactly one charm
    for model_name, model_constraints in domain.models.items():
        for application in model_constraints.application_constraints.keys():
            solver.assert_and_track(
                z3.Sum(
                    [
                        z3.If(m, 1, 0)
                        for mapping, m in model_constraints.application_to_charm.items()
                        if mapping.application == application
                    ]
                    + [z3.IntVal(0)]
                )
                == 1,
                ApplicationExistsTag(model=model_name, application=application).encode(),
            )

    # Ensure each charm maps to at most one application
    for charm_id, charm in enumerate(domain.charms):
        solver.assert_and_track(
            z3.Sum(
                [z3.If(m, 1, 0) for mapping, m in app_to_charm.items() if mapping.charm_id == charm_id] + [z3.IntVal(0)]
            )
            <= 1,
            CharmMappedToSingleApplicationTag(charm=_charm_payload(charm, charm_id)).encode(),
        )

    # Ensure charm exists if application-to-charm mapping is active
    for mapping, mapping_var in app_to_charm.items():
        charm_var = domain.charms[mapping.charm_id].exists
        solver.assert_and_track(
            z3.Implies(mapping_var, charm_var),
            CharmExistsFromApplicationTag(
                application=mapping.application,
                charm=_charm_payload(domain.charms[mapping.charm_id], mapping.charm_id),
            ).encode(),
        )

    # Ensure each user-specified application integration maps to exactly one charm integration
    for model_name, model_constraints in domain.models.items():
        for app_integration in model_constraints.integration_constraints:
            solver.assert_and_track(
                z3.Sum(
                    [
                        z3.If(m, 1, 0)
                        for (a_int, c_int), m in model_constraints.application_integration_to_charm_integration.items()
                        if a_int == app_integration
                    ]
                    + [z3.IntVal(0)]
                )
                == 1,
                ApplicationIntegrationExistsTag(
                    model=model_name,
                    integration=_app_endpoints_from_integration(app_integration),
                ).encode(),
            )

    # Ensure each charm integration maps to at most one application integration
    for charm_integration in domain.charm_integrations.keys():
        solver.assert_and_track(
            z3.Sum(
                [z3.If(m, 1, 0) for (a_int, c_int), m in app_int_to_charm_int.items() if c_int == charm_integration]
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
    ), mapping_var in app_int_to_charm_int.items():
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
    ), mapping_var in app_int_to_charm_int.items():
        # ApplicationIntegration is unordered, CharmIntegration is ordered
        # Find the correct application-to-charm mappings by checking which actually exist
        charm_req = charm_integration.requires_endpoint
        charm_prov = charm_integration.provides_endpoint

        # Try both orderings: (req_app, prov_app, req_endpoint, prov_endpoint)
        for req_app_ep, prov_app_ep in [
            (
                app_integration.endpoint_1,
                app_integration.endpoint_2,
            ),
            (
                app_integration.endpoint_2,
                app_integration.endpoint_1,
            ),
        ]:
            req_mapping_key = ApplicationToCharmMapping(application=req_app_ep.application, charm_id=charm_req.charm_id)
            prov_mapping_key = ApplicationToCharmMapping(
                application=prov_app_ep.application, charm_id=charm_prov.charm_id
            )

            if req_mapping_key in app_to_charm and prov_mapping_key in app_to_charm:
                solver.assert_and_track(
                    z3.Implies(
                        mapping_var,
                        z3.And(app_to_charm[req_mapping_key], app_to_charm[prov_mapping_key]),
                    ),
                    ApplicationIntegrationAppsMapToCharmsTag(
                        application_integration=_app_endpoints_from_integration(app_integration),
                        charm_integration=_charm_endpoints_from_integration(charm_integration, domain),
                    ).encode(),
                )
                break
        else:
            raise ValueError(
                f"Integration mapping exists but application-to-charm mappings don't exist: "
                f"{app_integration} -> {charm_integration}"
            )


def add_charm_constraints(solver: z3.Solver, domain: Domain) -> None:
    # Snapshot aggregated mapping once to avoid rebuilding the dict in nested loops.
    app_to_charm = {k: v for mc in domain.models.values() for k, v in mc.application_to_charm.items()}

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

    # Build a lookup of cross-model integration counts per (application, endpoint).
    # When the solver assigns an application to a charm, the charm's endpoint count
    # must include these external integrations.
    cmr_counts: dict[tuple[str, str], int] = {}
    for cmr in (c for mc in domain.models.values() for c in mc.cross_model_constraints):
        key = (cmr.local_application, cmr.local_endpoint)
        cmr_counts[key] = cmr_counts.get(key, 0) + 1
        # When the remote model is also in this domain, the provider endpoint
        # also gets a +1 so its non-optional constraint is considered satisfied.
        if cmr.remote.model in domain.models:
            remote_key = (cmr.remote.application, cmr.remote.endpoint)
            cmr_counts[remote_key] = cmr_counts.get(remote_key, 0) + 1

    # Block application-to-charm combinations where a user-specified CMR would have
    # mismatched interfaces. When both sides of the CMR are in this domain we can
    # enumerate all (local_charm, remote_charm) pairs; if their endpoint interfaces
    # differ the solver must not pick that combination.
    for model_name, model_constraints in domain.models.items():
        for cmr in model_constraints.cross_model_constraints:
            remote_model = cmr.remote.model
            if remote_model not in domain.models:
                continue  # external CMR - no remote charm metadata available
            remote_model_constraints = domain.models[remote_model]
            for local_mapping, local_var in model_constraints.application_to_charm.items():
                if local_mapping.application != cmr.local_application:
                    continue
                local_ep = domain.charms[local_mapping.charm_id].spec.endpoints.get(cmr.local_endpoint)
                if local_ep is None:
                    continue
                for remote_mapping, remote_var in remote_model_constraints.application_to_charm.items():
                    if remote_mapping.application != cmr.remote.application:
                        continue
                    remote_ep = domain.charms[remote_mapping.charm_id].spec.endpoints.get(cmr.remote.endpoint)
                    if remote_ep is None:
                        continue
                    if local_ep.interface != remote_ep.interface:
                        solver.add(z3.Not(z3.And(local_var, remote_var)))

    # Ensure endpoint count equals number of integrations using that endpoint
    for charm_id, charm in enumerate(domain.charms):
        for endpoint_name, endpoint in charm.endpoints.items():
            integrations_using_endpoint: list[z3.BoolRef] = []
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

            # Add cross-model contributions: for each (app, endpoint) that has CMR
            # integrations, add +N when the application-to-charm mapping is active.
            cmr_terms: list[z3.ArithRef] = []
            for (app, ep), ext_count in cmr_counts.items():
                if ep != endpoint_name:
                    continue
                mapping_key = ApplicationToCharmMapping(application=app, charm_id=charm_id)
                if mapping_key in app_to_charm:
                    cmr_terms.append(z3.If(app_to_charm[mapping_key], ext_count, 0))

            # Add PotentialCMR contributions: each active PotentialCMR that
            # involves this charm_id and endpoint_name adds +1 to the count.
            potential_cmr_terms: list[z3.ArithRef] = []
            for pcmr in domain.potential_cmrs:
                if (pcmr.requires_charm_id == charm_id and pcmr.requires_endpoint == endpoint_name) or (
                    pcmr.provides_charm_id == charm_id and pcmr.provides_endpoint == endpoint_name
                ):
                    potential_cmr_terms.append(z3.If(pcmr.exists, 1, 0))

            all_terms = cmr_terms + potential_cmr_terms
            num_terms = len(integrations_using_endpoint) + len(all_terms)
            count_expr = z3.Sum([z3.If(i, 1, 0) for i in integrations_using_endpoint] + all_terms + [z3.IntVal(0)])
            solver.assert_and_track(
                endpoint.count == count_expr,
                EndpointCountMatchesIntegrationsTag(
                    charm=_charm_endpoint_payload(charm, charm_id, endpoint_name),
                    num_terms=num_terms,
                ).encode(),
            )
            # Link integrated boolean to count
            solver.assert_and_track(
                endpoint.integrated == (endpoint.count >= 1),
                EndpointIntegratedMatchesCountTag(
                    charm=_charm_endpoint_payload(charm, charm_id, endpoint_name)
                ).encode(),
            )

    # PotentialCMR constraints: both charms must exist if CMR is active
    for pcmr in domain.potential_cmrs:
        req_charm = domain.charms[pcmr.requires_charm_id]
        prov_charm = domain.charms[pcmr.provides_charm_id]
        solver.add(z3.Implies(pcmr.exists, z3.And(req_charm.exists, prov_charm.exists)))


def add_charm_metadata_constraints(solver: z3.Solver, domain: Domain) -> None:
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

    # Coherence: when an integration exists, a feature can only be active on one endpoint
    # if the other endpoint also declares that feature.  This prevents endpoints with
    # non-overlapping feature sets from activating mismatched features.
    for charm_integration, integration_domain in domain.charm_integrations.items():
        req_ep = domain.charms[charm_integration.requires_endpoint.charm_id].endpoints[
            charm_integration.requires_endpoint.endpoint
        ]
        prov_ep = domain.charms[charm_integration.provides_endpoint.charm_id].endpoints[
            charm_integration.provides_endpoint.endpoint
        ]
        for f, f_var in req_ep.features.items():
            if f not in prov_ep.features:
                solver.add(z3.Implies(integration_domain.exists, z3.Not(f_var)))
            else:
                # Both endpoints declare this feature: they must agree when integrated.
                solver.add(z3.Implies(integration_domain.exists, f_var == prov_ep.features[f]))
        for f, f_var in prov_ep.features.items():
            if f not in req_ep.features:
                solver.add(z3.Implies(integration_domain.exists, z3.Not(f_var)))

    # Config domain constraints: when a charm exists, its config variable must equal
    # one of the declared allowed values.
    for charm in domain.charms:
        for key, cfg in charm.config.items():
            if cfg.var is None:
                continue
            allowed = [v for v in charm.spec.configs[key] if v is not None]
            if allowed:
                value_constraint = z3.Or([config_value_to_z3(cfg.var, v) for v in allowed])
                if cfg.isset_var is not None:
                    # Config is optional (None is an allowed value).  The value constraint
                    # only applies when is_set is True; when is_set is False the value var
                    # is unconstrained (solver may choose anything, but set() will be False).
                    solver.add(z3.Implies(z3.And(charm.exists, cfg.isset_var), value_constraint))
                else:
                    # Config is always required when the charm exists.
                    solver.add(z3.Implies(charm.exists, value_constraint))

    # DSL custom constraints from override files.
    for charm_id, charm in enumerate(domain.charms):
        if not charm.spec.constraints:
            continue
        ctx = LoweringContext(charm_id=charm_id, domain_charm=charm, domain=domain)
        for idx, expr in enumerate(charm.spec.constraints):
            try:
                result = lower(expr, ctx)
            except DSLLoweringError as e:
                raise ValueError(f"Failed to lower constraint {idx} for charm {charm.spec.name!r}: {e}") from e
            solver.assert_and_track(
                z3.Implies(charm.exists, result.expr),
                CharmCustomConstraintTag(charm=_charm_payload(charm, charm_id), assertion_idx=idx).encode(),
            )
            for sub in result.sub_assertions:
                solver.assert_and_track(
                    z3.Implies(charm.exists, sub.expr),
                    sub.tag.encode(),
                )


def add_charm_dependency_constraints(solver: z3.Solver, domain: Domain) -> None:
    # Create rank variables for topological ordering to prevent cycles
    charm_count = len(domain.charms)
    ranks: list[z3.ArithRef] = [z3.Int(f"charm_{idx}_rank") for idx in range(charm_count)]

    # Bound each rank to [0, charm_count]
    for charm_id, rank_var in enumerate(ranks):
        solver.assert_and_track(
            z3.And(rank_var >= 0, rank_var <= charm_count),
            CharmRankBoundedTag(charm=_charm_payload(domain.charms[charm_id], charm_id)).encode(),
        )

    # Enforce acyclic dependencies: requiring charm must have higher rank than providing charm
    # Skip if either endpoint is marked as cyclic (allows intentional cycles)
    for charm_integration, integration_var in domain.charm_integrations.items():
        # With semantic ordering, we can directly access requires and provides endpoints
        charm_req = charm_integration.requires_endpoint
        charm_prov = charm_integration.provides_endpoint

        # Look up endpoint specifications
        requires_spec = domain.charms[charm_req.charm_id].spec.endpoints[charm_req.endpoint]
        provides_spec = domain.charms[charm_prov.charm_id].spec.endpoints[charm_prov.endpoint]

        # Skip rank constraint if either endpoint is marked as cyclic (allows cycles)
        if requires_spec.cyclic or provides_spec.cyclic:
            continue

        # Assert: if integration exists, requiring charm must have higher rank than providing charm
        solver.assert_and_track(
            z3.Implies(integration_var.exists, ranks[charm_req.charm_id] > ranks[charm_prov.charm_id]),
            CharmDependencyCyclicTag(
                requiring_charm=_charm_endpoint_payload(
                    domain.charms[charm_req.charm_id], charm_req.charm_id, charm_req.endpoint
                ),
                providing_charm=_charm_endpoint_payload(
                    domain.charms[charm_prov.charm_id], charm_prov.charm_id, charm_prov.endpoint
                ),
            ).encode(),
        )

    # Enforce rank ordering on cross-model PotentialCMRs too, so two charms in
    # different models cannot form a cycle (e.g. charm A provides to B AND B
    # provides to A across models).
    for pcmr in domain.potential_cmrs:
        requires_spec = domain.charms[pcmr.requires_charm_id].spec.endpoints[pcmr.requires_endpoint]
        provides_spec = domain.charms[pcmr.provides_charm_id].spec.endpoints[pcmr.provides_endpoint]

        if requires_spec.cyclic or provides_spec.cyclic:
            continue

        solver.assert_and_track(
            z3.Implies(pcmr.exists, ranks[pcmr.requires_charm_id] > ranks[pcmr.provides_charm_id]),
            CharmDependencyCyclicTag(
                requiring_charm=_charm_endpoint_payload(
                    domain.charms[pcmr.requires_charm_id], pcmr.requires_charm_id, pcmr.requires_endpoint
                ),
                providing_charm=_charm_endpoint_payload(
                    domain.charms[pcmr.provides_charm_id], pcmr.provides_charm_id, pcmr.provides_endpoint
                ),
            ).encode(),
        )


def add_constraints(solver: z3.Solver, domain: Domain) -> None:
    add_application_constraints(solver, domain)
    add_charm_constraints(solver, domain)
    add_charm_metadata_constraints(solver, domain)
    add_charm_dependency_constraints(solver, domain)
