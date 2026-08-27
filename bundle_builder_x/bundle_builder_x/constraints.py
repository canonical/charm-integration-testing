# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

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
    IntegrationFeatureMismatchTag,
    SubordinateBaseMismatchTag,
)
from .charm import EndpointScope
from .domain import (
    Domain,
    DomainApplicationIntegration,
    DomainCharm,
    DomainCharmIntegration,
    ModelRef,
)
from .dsl_lowering import DSLLoweringError, LoweringContext, config_value_to_z3, lower


def _app_endpoints_from_integration(integration: DomainApplicationIntegration) -> list[AppEndpointPayload]:
    return [
        AppEndpointPayload(
            application=integration.endpoint_1.application,
            endpoint=integration.endpoint_1.endpoint,
            model=integration.endpoint_1.model,
        ),
        AppEndpointPayload(
            application=integration.endpoint_2.application,
            endpoint=integration.endpoint_2.endpoint,
            model=integration.endpoint_2.model,
        ),
    ]


def _charm_payload(charm: DomainCharm, charm_id: int) -> CharmPayload:
    return CharmPayload(charm_name=charm.spec.name, charm_id=charm_id)


def _charm_endpoint_payload(charm: DomainCharm, charm_id: int, endpoint: str | None) -> CharmEndpointPayload:
    return CharmEndpointPayload(charm_name=charm.spec.name, charm_id=charm_id, endpoint=endpoint)


def _charm_endpoints_from_integration(integration: DomainCharmIntegration) -> list[CharmEndpointPayload]:
    return [
        CharmEndpointPayload(
            charm_name="", charm_id=integration.requires_charm_id, endpoint=integration.requires_endpoint
        ),
        CharmEndpointPayload(
            charm_name="", charm_id=integration.provides_charm_id, endpoint=integration.provides_endpoint
        ),
    ]


def add_application_constraints(solver: z3.Solver, domain: Domain) -> None:
    # Snapshot aggregated mappings once to avoid rebuilding dicts in loops.
    # Flat (app_name, charm_id) -> BoolRef for cross-model lookups.
    app_to_charm: dict[tuple[str, int], z3.BoolRef] = {
        (app, cid): var
        for mc in domain.models.values()
        for app, domain_app in mc.applications.items()
        for cid, var in domain_app.charm_ids.items()
    }

    # Ensure each application maps to exactly one charm
    for model_ref, model_constraints in domain.models.items():
        for application, domain_app in model_constraints.applications.items():
            charm_vars = list(domain_app.charm_ids.values())
            solver.assert_and_track(
                z3.Sum([z3.If(m, 1, 0) for m in charm_vars] + [z3.IntVal(0)]) == 1,
                ApplicationExistsTag(model=model_ref, application=application).encode(),
            )

    # Ensure each charm maps to at most one application
    for charm_id, charm in enumerate(domain.charms):
        terms = [var for (_, cid), var in app_to_charm.items() if cid == charm_id]
        solver.assert_and_track(
            z3.Sum([z3.If(m, 1, 0) for m in terms] + [z3.IntVal(0)]) <= 1,
            CharmMappedToSingleApplicationTag(charm=_charm_payload(charm, charm_id)).encode(),
        )

    # Ensure charm exists if application-to-charm mapping is active
    for (app, cid), mapping_var in app_to_charm.items():
        charm_var = domain.charms[cid].exists
        solver.assert_and_track(
            z3.Implies(mapping_var, charm_var),
            CharmExistsFromApplicationTag(
                application=app,
                charm=_charm_payload(domain.charms[cid], cid),
            ).encode(),
        )

    # Ensure each user-specified integration (local or CMR) maps to exactly one charm integration.
    # For external CMRs (remote model not in domain), skip: those are handled via cmr_counts.
    for model_ref, model_constraints in domain.models.items():
        for app_integration in model_constraints.application_integrations:
            is_cmr = app_integration.endpoint_1.model != app_integration.endpoint_2.model
            if is_cmr:
                remote_model = (
                    app_integration.endpoint_1.model
                    if app_integration.endpoint_1.model != ModelRef()
                    else app_integration.endpoint_2.model
                )
                if remote_model not in domain.models:
                    continue  # external CMR - satisfied via cmr_counts in add_charm_constraints
            charm_int_dict = app_integration.charm_integration_ids
            solver.assert_and_track(
                z3.Sum([z3.If(m, 1, 0) for m in charm_int_dict.values()] + [z3.IntVal(0)]) == 1,
                ApplicationIntegrationExistsTag(
                    model=model_ref,
                    integration=_app_endpoints_from_integration(app_integration),
                ).encode(),
            )

    # Ensure each charm integration maps to at most one application integration.
    for i_idx, integration in enumerate(domain.charm_integrations):
        terms = [
            z3.If(var, 1, 0)
            for mc in domain.models.values()
            for app_int in mc.application_integrations
            for idx, var in app_int.charm_integration_ids.items()
            if idx == i_idx
        ]
        solver.assert_and_track(
            z3.Sum(terms + [z3.IntVal(0)]) <= 1,
            CharmIntegrationMappedToSingleApplicationIntegrationTag(
                charm_integration=_charm_endpoints_from_integration(integration)
            ).encode(),
        )

    # Ensure charm integration exists if application-to-charm integration mapping is active.
    # Also ensure the relevant application-to-charm mappings are active.
    for model_ref, model_constraints in domain.models.items():
        for app_integration in model_constraints.application_integrations:
            for i_idx, mapping_var in app_integration.charm_integration_ids.items():
                integration = domain.charm_integrations[i_idx]

                solver.assert_and_track(
                    z3.Implies(mapping_var, integration.exists),
                    CharmIntegrationExistsFromApplicationIntegrationTag(
                        application_integration=_app_endpoints_from_integration(app_integration),
                        charm_integration=_charm_endpoints_from_integration(integration),
                    ).encode(),
                )

                # Force app-to-charm mappings active when the integration mapping is active.
                # Application endpoints are unordered, so we collect every valid ordering.
                # A valid ordering requires:
                #   1. Both app-to-charm keys exist in the domain.
                #   2. The app integration endpoint names match the charm integration's
                #      requires/provides endpoint names for the chosen assignment.
                valid_orderings: list[z3.BoolRef] = []
                seen_ordering_keys: set[tuple[tuple[str, int], tuple[str, int]]] = set()
                for req_app_ep, prov_app_ep in [
                    (app_integration.endpoint_1, app_integration.endpoint_2),
                    (app_integration.endpoint_2, app_integration.endpoint_1),
                ]:
                    if (
                        req_app_ep.endpoint != integration.requires_endpoint
                        or prov_app_ep.endpoint != integration.provides_endpoint
                    ):
                        continue

                    req_key = (req_app_ep.application, integration.requires_charm_id)
                    prov_key = (prov_app_ep.application, integration.provides_charm_id)

                    if req_key in app_to_charm and prov_key in app_to_charm:
                        ordering_key = (req_key, prov_key)
                        if ordering_key not in seen_ordering_keys:
                            seen_ordering_keys.add(ordering_key)
                            valid_orderings.append(z3.And(app_to_charm[req_key], app_to_charm[prov_key]))

                if valid_orderings:
                    solver.assert_and_track(
                        z3.Implies(mapping_var, z3.Or(*valid_orderings)),
                        ApplicationIntegrationAppsMapToCharmsTag(
                            application_integration=_app_endpoints_from_integration(app_integration),
                            charm_integration=_charm_endpoints_from_integration(integration),
                        ).encode(),
                    )
                else:
                    raise ValueError(
                        f"Integration mapping exists but application-to-charm mappings don't exist: "
                        f"{app_integration} -> {integration}"
                    )


def add_charm_constraints(solver: z3.Solver, domain: Domain) -> None:
    # Snapshot aggregated mapping once to avoid rebuilding the dict in nested loops.
    app_to_charm: dict[tuple[str, int], z3.BoolRef] = {
        (app, cid): var
        for mc in domain.models.values()
        for app, domain_app in mc.applications.items()
        for cid, var in domain_app.charm_ids.items()
    }

    # Ensure both charms exist if integration exists (local and cross-model)
    for integration in domain.charm_integrations:
        for charm_id in [integration.requires_charm_id, integration.provides_charm_id]:
            charm_var = domain.charms[charm_id].exists
            solver.assert_and_track(
                z3.Implies(integration.exists, charm_var),
                CharmExistsFromIntegrationTag(
                    charm=_charm_payload(domain.charms[charm_id], charm_id),
                    integration=_charm_endpoints_from_integration(integration),
                ).encode(),
            )

    # Build a lookup of cross-model integration counts per (application, endpoint).
    # Only covers external CMRs - in-domain CMRs have their endpoint count handled
    # through DomainCharmIntegration.exists (forced True by the user-CMR mapping constraint).
    cmr_counts: dict[tuple[str, str], int] = {}
    for mc in domain.models.values():
        for app_int in mc.application_integrations:
            # Identify external CMR: one endpoint has a model that is NOT in the domain
            ep1_model = app_int.endpoint_1.model
            ep2_model = app_int.endpoint_2.model
            if ep1_model == ep2_model:
                continue  # local integration
            if (ep1_model if ep1_model != ModelRef() else ep2_model) in domain.models:
                continue  # in-domain CMR - endpoint count flows through integration.exists
            local_ep = app_int.endpoint_1 if app_int.endpoint_1.model == ModelRef() else app_int.endpoint_2
            key = (local_ep.application, local_ep.endpoint)
            cmr_counts[key] = cmr_counts.get(key, 0) + 1

    # Ensure endpoint count equals number of integrations using that endpoint
    for charm_id, charm in enumerate(domain.charms):
        for endpoint_name, endpoint in charm.endpoints.items():
            integrations_using_endpoint: list[z3.BoolRef] = []
            for integration in domain.charm_integrations:
                if (integration.requires_charm_id == charm_id and integration.requires_endpoint == endpoint_name) or (
                    integration.provides_charm_id == charm_id and integration.provides_endpoint == endpoint_name
                ):
                    integrations_using_endpoint.append(integration.exists)

            # Add cross-model contributions: for each (app, endpoint) that has CMR
            # integrations, add +N when the application-to-charm mapping is active.
            cmr_terms: list[z3.ArithRef] = []
            for (app, ep), ext_count in cmr_counts.items():
                if ep != endpoint_name:
                    continue
                mapping_var = app_to_charm.get((app, charm_id))
                if mapping_var is not None:
                    cmr_terms.append(z3.If(mapping_var, ext_count, 0))

            num_terms = len(integrations_using_endpoint) + len(cmr_terms)
            count_expr = z3.Sum([z3.If(i, 1, 0) for i in integrations_using_endpoint] + cmr_terms + [z3.IntVal(0)])
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


def add_charm_metadata_constraints(solver: z3.Solver, domain: Domain) -> None:
    # Unit count lower bound: when a charm exists it must have at least one unit.
    for charm in domain.charms:
        solver.add(z3.Implies(charm.exists, charm.num_units >= 1))

    # Ensure non-optional endpoints have at least one integration if charm exists
    for charm_id, charm in enumerate(domain.charms):
        for endpoint_name, spec_endpoint in charm.spec.endpoints.items():
            if not spec_endpoint.optional:
                solver.assert_and_track(
                    z3.Implies(charm.exists, charm.endpoints[endpoint_name].count >= 1),
                    CharmEndpointNonOptionalTag(
                        charm=_charm_endpoint_payload(charm, charm_id, endpoint_name),
                        interface=spec_endpoint.interface,
                    ).encode(),
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
    # Applied uniformly to local and cross-model integrations: a relation's feature
    # requirements don't change just because the two charms land in different models
    # (see SQT-1038 - cross-model integrations previously skipped this check, letting
    # the solver silently pair charms whose declared features didn't actually match).
    for integration in domain.charm_integrations:
        requires_charm = domain.charms[integration.requires_charm_id]
        provides_charm = domain.charms[integration.provides_charm_id]
        req_ep = requires_charm.endpoints[integration.requires_endpoint]
        prov_ep = provides_charm.endpoints[integration.provides_endpoint]
        requires_payload = _charm_endpoint_payload(
            requires_charm, integration.requires_charm_id, integration.requires_endpoint
        )
        provides_payload = _charm_endpoint_payload(
            provides_charm, integration.provides_charm_id, integration.provides_endpoint
        )
        for f, f_var in req_ep.features.items():
            if f not in prov_ep.features:
                solver.assert_and_track(
                    z3.Implies(integration.exists, z3.Not(f_var)),
                    IntegrationFeatureMismatchTag(
                        requires=requires_payload,
                        provides=provides_payload,
                        feature=f,
                    ).encode(),
                )
            else:
                # Both endpoints declare this feature: they must agree when integrated.
                solver.assert_and_track(
                    z3.Implies(integration.exists, f_var == prov_ep.features[f]),
                    IntegrationFeatureMismatchTag(
                        requires=requires_payload,
                        provides=provides_payload,
                        feature=f,
                    ).encode(),
                )
        for f, f_var in prov_ep.features.items():
            if f not in req_ep.features:
                solver.assert_and_track(
                    z3.Implies(integration.exists, z3.Not(f_var)),
                    IntegrationFeatureMismatchTag(
                        requires=requires_payload,
                        provides=provides_payload,
                        feature=f,
                    ).encode(),
                )

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

    # Resource domain constraints: when a charm exists, its resource variable must equal
    # one of the declared allowed values.  Resources are always strings.
    for charm in domain.charms:
        for key, res in charm.resources.items():
            if res.var is None:
                continue
            res_allowed = [v for v in charm.spec.resources[key] if v is not None]
            if res_allowed:
                value_constraint = z3.Or([res.var == z3.StringVal(v) for v in res_allowed])
                if res.isset_var is not None:
                    # Resource is optional (None is an allowed value).  The value constraint
                    # only applies when isset_var is True; when isset_var is False the value var
                    # is unconstrained (solver may choose anything, but set() will be False).
                    solver.add(z3.Implies(z3.And(charm.exists, res.isset_var), value_constraint))
                else:
                    # Resource is always required when the charm exists.
                    solver.add(z3.Implies(charm.exists, value_constraint))

    # DSL custom constraints from override files.
    for charm_id, charm in enumerate(domain.charms):
        if not charm.spec.constraints:
            continue
        ctx = LoweringContext(charm_id=charm_id, domain_charm=charm, domain=domain)
        # Collect sub-assertion tags across all constraints for this charm to
        # avoid duplicate named assertions.  Two different constraints can
        # legitimately emit the same expansion hint (e.g. "re-fetch charm X on
        # track 8"); adding it once is sufficient.
        seen_sub_tags: set[str] = set()
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
                tag = sub.tag.encode()
                if tag in seen_sub_tags:
                    continue
                seen_sub_tags.add(tag)
                solver.assert_and_track(
                    z3.Implies(charm.exists, sub.expr),
                    tag,
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
    for integration in domain.charm_integrations:
        requires_spec = domain.charms[integration.requires_charm_id].spec.endpoints[integration.requires_endpoint]
        provides_spec = domain.charms[integration.provides_charm_id].spec.endpoints[integration.provides_endpoint]

        if requires_spec.cyclic or provides_spec.cyclic:
            continue

        solver.assert_and_track(
            z3.Implies(
                integration.exists,
                ranks[integration.requires_charm_id] > ranks[integration.provides_charm_id],
            ),
            CharmDependencyCyclicTag(
                requiring_charm=_charm_endpoint_payload(
                    domain.charms[integration.requires_charm_id],
                    integration.requires_charm_id,
                    integration.requires_endpoint,
                ),
                providing_charm=_charm_endpoint_payload(
                    domain.charms[integration.provides_charm_id],
                    integration.provides_charm_id,
                    integration.provides_endpoint,
                ),
            ).encode(),
        )


def add_subordinate_constraints(solver: z3.Solver, domain: Domain) -> None:
    """Enforce base matching for container-scoped integrations (subordinate-principal).

    In Juju, a subordinate charm must share the same base (Ubuntu version) as its
    principal. Container-scoped integrations indicate a subordinate relationship.
    When such an integration is active, both charms must have the same ubuntu_version.
    """
    for integration in domain.charm_integrations:
        req_charm = domain.charms[integration.requires_charm_id]
        prov_charm = domain.charms[integration.provides_charm_id]
        req_endpoint = req_charm.spec.endpoints[integration.requires_endpoint]
        prov_endpoint = prov_charm.spec.endpoints[integration.provides_endpoint]

        # A container-scoped endpoint means this is a subordinate-principal relationship.
        # Conventionally the subordinate requires with scope:container, but handle the
        # reverse (provides side has container scope) for completeness.
        if req_endpoint.scope != EndpointScope.CONTAINER and prov_endpoint.scope != EndpointScope.CONTAINER:
            continue

        # Identify subordinate and principal
        if req_endpoint.scope == EndpointScope.CONTAINER:
            sub_charm, sub_id = req_charm, integration.requires_charm_id
            sub_endpoint = integration.requires_endpoint
            principal_charm, principal_id = prov_charm, integration.provides_charm_id
            principal_endpoint = integration.provides_endpoint
        else:
            sub_charm, sub_id = prov_charm, integration.provides_charm_id
            sub_endpoint = integration.provides_endpoint
            principal_charm, principal_id = req_charm, integration.requires_charm_id
            principal_endpoint = integration.requires_endpoint

        # If both charms have the same base already, no constraint needed
        if sub_charm.spec.ubuntu_version == principal_charm.spec.ubuntu_version:
            continue

        # When the integration is active, the bases must match - but they don't.
        # This is a hard contradiction: assert that this integration cannot exist.
        solver.assert_and_track(
            z3.Not(integration.exists),
            SubordinateBaseMismatchTag(
                subordinate_charm_name=sub_charm.spec.name,
                subordinate_charm_id=sub_id,
                subordinate_endpoint=sub_endpoint,
                principal_charm_name=principal_charm.spec.name,
                principal_charm_id=principal_id,
                principal_endpoint=principal_endpoint,
                subordinate_base=sub_charm.spec.ubuntu_version,
                principal_base=principal_charm.spec.ubuntu_version,
            ).encode(),
        )


def add_constraints(solver: z3.Solver, domain: Domain) -> None:
    add_application_constraints(solver, domain)
    add_charm_constraints(solver, domain)
    add_charm_metadata_constraints(solver, domain)
    add_charm_dependency_constraints(solver, domain)
    add_subordinate_constraints(solver, domain)
