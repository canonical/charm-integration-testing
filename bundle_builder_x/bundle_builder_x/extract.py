# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

import z3  # type: ignore[import-untyped]

from .bundle import Application, ApplicationEndpoint, Bundle, CrossModelIntegration, Integration, Solution
from .charm import EndpointType
from .domain import Domain, ModelRef


def _extract_single_model(
    model: z3.ModelRef,
    domain: Domain,
    model_ref: ModelRef,
    logger: logging.Logger,
) -> tuple[Bundle, dict[int, str]]:
    """Extract a Bundle for a single model from the global Z3 solution.

    Returns the Bundle and a mapping of charm_id -> application name for
    cross-referencing discovered CMRs.
    """
    mc = domain.models[model_ref]

    # Find existing charms in this model
    existing_charm_ids = [
        cid
        for cid in range(len(domain.charms))
        if domain.charms[cid].model == model_ref and model.evaluate(domain.charms[cid].exists, model_completion=True)
    ]

    charm_id_to_app_name: dict[int, str] = {}
    used_names: set[str] = set()

    for charm_id in existing_charm_ids:
        for app_name, domain_app in mc.applications.items():
            mapping_var = domain_app.charm_ids.get(charm_id)
            if mapping_var is not None and model.evaluate(mapping_var, model_completion=True):
                charm_id_to_app_name[charm_id] = app_name
                used_names.add(app_name)
                logger.info(
                    f"[{model_ref.key}] Application {app_name} mapped to charm "
                    f"{domain.charms[charm_id].spec.name} (id={charm_id})"
                )
                break

    for charm_id in existing_charm_ids:
        if charm_id in charm_id_to_app_name:
            continue
        base_name = domain.charms[charm_id].spec.name
        app_name = base_name
        suffix_idx = 0
        while app_name in used_names:
            # Generate suffixes: a-z, then aa, ab, ..., az, ba, ...
            chars = []
            n = suffix_idx
            while True:
                chars.append(chr(ord("a") + n % 26))
                n = n // 26 - 1
                if n < 0:
                    break
            app_name = f"{base_name}-{''.join(reversed(chars))}"
            suffix_idx += 1
        charm_id_to_app_name[charm_id] = app_name
        used_names.add(app_name)

    applications = {}
    for charm_id, app_name in charm_id_to_app_name.items():
        charm = domain.charms[charm_id]
        config: dict[str, object] = {}
        for key, cfg in charm.config.items():
            if cfg.fixed_value:
                config[key] = cfg.default
                continue
            if cfg.var is None:
                continue
            if cfg.isset_var is not None:
                is_set = model.evaluate(cfg.isset_var, model_completion=True)
                if not is_set:
                    continue
            raw = model.evaluate(cfg.var, model_completion=True)
            if cfg.bool_as_int and z3.is_int_value(raw):
                # See DomainCharmConfig.bool_as_int.
                val: object = bool(raw.as_long())
            elif z3.is_string_value(raw):
                val = raw.as_string()
            elif z3.is_int_value(raw):
                val = raw.as_long()
            elif z3.is_bool(raw):
                val = bool(raw)
            elif z3.is_rational_value(raw):
                val = float(raw.as_decimal(10).rstrip("?"))
            else:
                continue
            if cfg.default == val:
                continue
            config[key] = val

        resources: dict[str, str] = {}
        for key, res in charm.resources.items():
            if res.fixed_value:
                if res.default is not None:
                    resources[key] = res.default
                continue
            if res.var is None:
                continue
            if res.isset_var is not None:
                is_set = model.evaluate(res.isset_var, model_completion=True)
                if not is_set:
                    continue
            raw = model.evaluate(res.var, model_completion=True)
            if z3.is_string_value(raw):
                resources[key] = raw.as_string()

        raw_units = model.evaluate(charm.num_units, model_completion=True)
        num_units = int(raw_units.as_long()) if z3.is_int_value(raw_units) else 1

        applications[app_name] = Application(charm=charm.spec, num_units=num_units, config=config, resources=resources)

    integrations = set()
    for integration in domain.charm_integrations:
        if domain.is_cross_model(integration):
            continue
        if not model.evaluate(integration.exists, model_completion=True):
            continue

        charm_id_1 = integration.requires_charm_id
        endpoint_1 = integration.requires_endpoint
        charm_id_2 = integration.provides_charm_id
        endpoint_2 = integration.provides_endpoint

        # Only include if both charms belong to this model
        if charm_id_1 not in charm_id_to_app_name or charm_id_2 not in charm_id_to_app_name:
            continue

        app_name_1 = charm_id_to_app_name[charm_id_1]
        app_name_2 = charm_id_to_app_name[charm_id_2]

        integrations.add(
            Integration.create(
                ApplicationEndpoint(application=app_name_1, endpoint=endpoint_1),
                ApplicationEndpoint(application=app_name_2, endpoint=endpoint_2),
            )
        )

    # Build cross-model integrations from integration constraints with a non-None model
    cross_model_integrations: list[CrossModelIntegration] = []
    for app_int in mc.application_integrations:
        if app_int.endpoint_1.model == app_int.endpoint_2.model:
            continue  # local integration
        local_ep = app_int.endpoint_1 if app_int.endpoint_1.model == ModelRef() else app_int.endpoint_2
        remote_ep = app_int.endpoint_2 if app_int.endpoint_1.model == ModelRef() else app_int.endpoint_1
        app = applications.get(local_ep.application)
        if app is None:
            continue
        charm_ep = app.charm.endpoints.get(local_ep.endpoint)
        if charm_ep is None:
            logger.warning(
                f"Cross-model integration local endpoint '{local_ep.endpoint}' "
                f"not found on charm '{app.charm.name}' for application '{local_ep.application}'"
            )
            continue
        remote_model_ref = remote_ep.model
        if remote_model_ref is None:
            continue  # shouldn't happen, but defensive
        # For REQUIRES: synthesize the saas URL pointing at the remote (providing) model.
        # For PROVIDES: always None; the mirror pass synthesizes the URL when creating
        # the REQUIRES entry.
        url: str | None
        if charm_ep.type == EndpointType.REQUIRES:
            if app_int.url is not None:
                url = app_int.url
            else:
                remote_mc = domain.models.get(remote_model_ref)
                url = (
                    f"{remote_mc.ref.controller}:{remote_mc.admin}/{remote_model_ref.name}.{app_int.offer_name}"
                    if remote_mc is not None
                    and remote_mc.ref.controller is not None
                    and remote_model_ref.name is not None
                    else None
                )
        else:
            url = None
        cross_model_integrations.append(
            CrossModelIntegration(
                local=ApplicationEndpoint(
                    application=local_ep.application,
                    endpoint=local_ep.endpoint,
                ),
                local_role=charm_ep.type,
                remote_model=remote_model_ref.key,
                remote_application=remote_ep.application,
                remote_endpoint=remote_ep.endpoint,
                offer_name=app_int.offer_name,
                url=url,
            )
        )

    bundle = Bundle(
        model=model_ref.key,
        controller=mc.ref.controller,
        admin=mc.admin,
        applications=applications,
        integrations=integrations,
        cross_model_integrations=cross_model_integrations,
        platform=mc.platform,
        arch=mc.arch,
        juju_version=mc.juju_version,
    )

    return bundle, charm_id_to_app_name


def _mirror_cmr_entries(
    bundles: dict[ModelRef, Bundle],
    bundles_by_key: dict[str, Bundle],
    from_role: EndpointType,
    to_role: EndpointType,
) -> None:
    """Add mirrored CMR entries to the remote bundle for all CMRs with ``from_role``.

    For PROVIDES→REQUIRES mirrors: synthesizes the saas URL from the source bundle's
    controller/admin/model.
    For REQUIRES→PROVIDES mirrors: URL is always None (PROVIDES entries are never exported).
    """
    for model_ref, bundle in list(bundles.items()):
        model_name = model_ref.key
        for cmr in bundle.cross_model_integrations:
            if cmr.local_role != from_role:
                continue
            if cmr.remote_model not in bundles_by_key:
                continue
            remote_bundle = bundles_by_key[cmr.remote_model]
            already_present = any(
                c.local_role == to_role
                and c.local.application == cmr.remote_application
                and c.local.endpoint == cmr.remote_endpoint
                and c.remote_model == model_name
                and c.remote_application == cmr.local.application
                and c.remote_endpoint == cmr.local.endpoint
                and c.offer_name == cmr.offer_name
                for c in remote_bundle.cross_model_integrations
            )
            if already_present:
                continue
            if remote_bundle.applications.get(cmr.remote_application) is None:
                continue
            if to_role == EndpointType.REQUIRES:
                url = (
                    f"{bundle.controller}:{bundle.admin}/{model_ref.name}.{cmr.offer_name}"
                    if bundle.controller is not None and model_ref.name is not None
                    else None
                )
            else:
                url = None
            remote_bundle.cross_model_integrations.append(
                CrossModelIntegration(
                    local=ApplicationEndpoint(
                        application=cmr.remote_application,
                        endpoint=cmr.remote_endpoint,
                    ),
                    local_role=to_role,
                    remote_model=model_name,
                    remote_application=cmr.local.application,
                    remote_endpoint=cmr.local.endpoint,
                    offer_name=cmr.offer_name,
                    url=url,
                )
            )


def extract_solution(
    z3_model: z3.ModelRef,
    domain: Domain,
    logger: logging.Logger,
) -> Solution:
    """Extract a Solution from the global Z3 model.

    Includes both user-specified CMRs and solver-discovered cross-model integrations.
    For discovered CMRs, URLs are synthesized from the domain's controller info
    where available.
    """
    bundles: dict[ModelRef, Bundle] = {}
    all_charm_maps: dict[ModelRef, dict[int, str]] = {}

    for model_ref in domain.models:
        bundle, charm_map = _extract_single_model(z3_model, domain, model_ref, logger)
        bundles[model_ref] = bundle
        all_charm_maps[model_ref] = charm_map

    # Add solver-discovered cross-model integrations to the relevant bundles.
    # Skip any integration that was forced active by a user-specified CMR: those
    # are already present in the bundle via _extract_single_model (which preserves
    # the user-specified URL and offer name).
    user_covered_idxs: set[int] = set()
    for mc in domain.models.values():
        for app_int in mc.application_integrations:
            is_cmr = app_int.endpoint_1.model != app_int.endpoint_2.model
            if not is_cmr:
                continue
            for i_idx, mapping_var in app_int.charm_integration_ids.items():
                if z3_model.evaluate(mapping_var, model_completion=True):
                    user_covered_idxs.add(i_idx)

    for i_idx, integration in enumerate(domain.charm_integrations):
        if not domain.is_cross_model(integration):
            continue
        if not z3_model.evaluate(integration.exists, model_completion=True):
            continue
        if i_idx in user_covered_idxs:
            continue  # user CMR already covers this; skip to avoid duplicate entries

        req_model_ref = domain.charms[integration.requires_charm_id].model
        prov_model_ref = domain.charms[integration.provides_charm_id].model
        req_app = all_charm_maps.get(req_model_ref, {}).get(integration.requires_charm_id)
        prov_app = all_charm_maps.get(prov_model_ref, {}).get(integration.provides_charm_id)

        if req_app is None or prov_app is None:
            logger.warning(
                f"Discovered CMR between {prov_model_ref.key}:{integration.provides_charm_id} and "
                f"{req_model_ref.key}:{integration.requires_charm_id} but application names could not be resolved"
            )
            continue

        interface = domain.integration_interface(integration)
        offer_name = domain.integration_offer_name(integration)

        logger.info(
            f"Discovered CMR: {prov_model_ref.key}.{prov_app}:{integration.provides_endpoint} "
            f"-> {req_model_ref.key}.{req_app}:{integration.requires_endpoint} "
            f"(interface: {interface})"
        )

        # Synthesize URL for discovered CMRs (REQUIRES side only)
        url: str | None = None
        prov_mc = domain.models.get(prov_model_ref)
        if prov_mc is not None and prov_mc.ref.controller is not None:
            url = f"{prov_mc.ref.controller}:{prov_mc.admin}/{prov_mc.ref.name}.{offer_name}"

        # Add REQUIRES side to the requiring model's bundle
        if req_model_ref in bundles:
            bundles[req_model_ref].cross_model_integrations.append(
                CrossModelIntegration(
                    local=ApplicationEndpoint(application=req_app, endpoint=integration.requires_endpoint),
                    local_role=EndpointType.REQUIRES,
                    remote_model=prov_model_ref.key,
                    remote_application=prov_app,
                    remote_endpoint=integration.provides_endpoint,
                    offer_name=offer_name,
                    url=url,
                )
            )

        # Add PROVIDES side to the providing model's bundle (no URL; never exported)
        if prov_model_ref in bundles:
            bundles[prov_model_ref].cross_model_integrations.append(
                CrossModelIntegration(
                    local=ApplicationEndpoint(application=prov_app, endpoint=integration.provides_endpoint),
                    local_role=EndpointType.PROVIDES,
                    remote_model=req_model_ref.key,
                    remote_application=req_app,
                    remote_endpoint=integration.requires_endpoint,
                    offer_name=offer_name,
                    url=None,
                )
            )

    # Mirror user-specified CMRs so both models in a pair always have a CMR entry:
    #   REQUIRES-side bundles gain a mirrored PROVIDES entry (for export_mermaid edges).
    #   PROVIDES-side bundles gain a mirrored REQUIRES entry (for the saas section).
    # Only applies to in-spec CMRs where the remote model is also in this solution.
    bundles_by_key: dict[str, Bundle] = {ref.key: b for ref, b in bundles.items()}
    _mirror_cmr_entries(bundles, bundles_by_key, from_role=EndpointType.REQUIRES, to_role=EndpointType.PROVIDES)
    _mirror_cmr_entries(bundles, bundles_by_key, from_role=EndpointType.PROVIDES, to_role=EndpointType.REQUIRES)

    return Solution(bundles=list(bundles.values()))
