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

import logging

import z3  # type: ignore[import-untyped]

from .bundle import Application, ApplicationEndpoint, Bundle, CrossModelIntegration, Integration, Solution
from .charm import EndpointType
from .domain import Domain


def _extract_single_model(
    model: z3.ModelRef,
    domain: Domain,
    model_name: str,
    logger: logging.Logger,
) -> tuple[Bundle, dict[int, str]]:
    """Extract a Bundle for a single model from the global Z3 solution.

    Returns the Bundle and a mapping of charm_id -> application name for
    cross-referencing discovered CMRs.
    """
    mc = domain.models[model_name]

    # Find existing charms in this model
    existing_charm_ids = [
        cid
        for cid in range(len(domain.charms))
        if domain.charm_to_model.get(cid) == model_name
        and model.evaluate(domain.charms[cid].exists, model_completion=True)
    ]

    charm_id_to_app_name: dict[int, str] = {}
    used_names: set[str] = set()

    for charm_id in existing_charm_ids:
        for mapping, mapping_var in mc.application_to_charm.items():
            if mapping.charm_id == charm_id and model.evaluate(mapping_var, model_completion=True):
                charm_id_to_app_name[charm_id] = mapping.application
                used_names.add(mapping.application)
                logger.info(
                    f"[{model_name}] Application {mapping.application} mapped to charm "
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
            if z3.is_string_value(raw):
                val: object = raw.as_string()
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

        applications[app_name] = Application(charm=charm.spec, config=config)

    integrations = set()
    for charm_integration, integration_var in domain.charm_integrations.items():
        if model.evaluate(integration_var.exists, model_completion=True):
            charm_id_1 = charm_integration.requires_endpoint.charm_id
            endpoint_1 = charm_integration.requires_endpoint.endpoint
            charm_id_2 = charm_integration.provides_endpoint.charm_id
            endpoint_2 = charm_integration.provides_endpoint.endpoint

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

    # Build cross-model integrations from user-specified CMR constraints
    cross_model_integrations: list[CrossModelIntegration] = []
    for cmr in mc.cross_model_constraints:
        app = applications.get(cmr.local_application)
        if app is None:
            continue
        charm_ep = app.charm.endpoints.get(cmr.local_endpoint)
        if charm_ep is None:
            logger.warning(
                f"Cross-model integration local endpoint '{cmr.local_endpoint}' "
                f"not found on charm '{app.charm.name}' for application '{cmr.local_application}'"
            )
            continue
        cross_model_integrations.append(
            CrossModelIntegration(
                local=ApplicationEndpoint(
                    application=cmr.local_application,
                    endpoint=cmr.local_endpoint,
                ),
                local_role=charm_ep.type,
                remote_model=cmr.remote.model,
                remote_application=cmr.remote.application,
                remote_endpoint=cmr.remote.endpoint,
                offer_name=cmr.remote.offer_name,
                url=cmr.remote.url,
            )
        )

    bundle = Bundle(
        model=model_name,
        controller=mc.controller,
        applications=applications,
        integrations=integrations,
        cross_model_integrations=cross_model_integrations,
        platform=mc.platform,
        arch=mc.arch,
        juju_version=mc.juju_version,
    )

    return bundle, charm_id_to_app_name


def extract_solution(
    z3_model: z3.ModelRef,
    domain: Domain,
    logger: logging.Logger,
) -> Solution:
    """Extract a Solution from the global Z3 model.

    Includes both user-specified CMRs and solver-discovered PotentialCMRs.
    For discovered CMRs, URLs are synthesized from the domain's controller info
    where available.
    """
    bundles: dict[str, Bundle] = {}
    all_charm_maps: dict[str, dict[int, str]] = {}

    for model_name in domain.models:
        bundle, charm_map = _extract_single_model(z3_model, domain, model_name, logger)
        bundles[model_name] = bundle
        all_charm_maps[model_name] = charm_map

    # Add solver-discovered PotentialCMRs to the relevant bundles
    for pcmr in domain.potential_cmrs:
        if not z3_model.evaluate(pcmr.exists, model_completion=True):
            continue

        req_model = pcmr.requires_model
        prov_model = pcmr.provides_model
        req_app = all_charm_maps.get(req_model, {}).get(pcmr.requires_charm_id)
        prov_app = all_charm_maps.get(prov_model, {}).get(pcmr.provides_charm_id)

        if req_app is None or prov_app is None:
            logger.warning(
                f"Discovered CMR between {prov_model}:{pcmr.provides_charm_id} and "
                f"{req_model}:{pcmr.requires_charm_id} but application names could not be resolved"
            )
            continue

        logger.info(
            f"Discovered CMR: {prov_model}.{prov_app}:{pcmr.provides_endpoint} "
            f"-> {req_model}.{req_app}:{pcmr.requires_endpoint} "
            f"(interface: {pcmr.interface})"
        )

        # Synthesize URL for discovered CMRs from the providing model's controller info
        url: str | None = None
        prov_mc = domain.models.get(prov_model)
        if prov_mc is not None and prov_mc.controller is not None:
            url = f"{prov_mc.controller}:admin/{prov_model}.{pcmr.offer_name}"

        # Add REQUIRES side to the requiring model's bundle
        if req_model in bundles:
            bundles[req_model].cross_model_integrations.append(
                CrossModelIntegration(
                    local=ApplicationEndpoint(application=req_app, endpoint=pcmr.requires_endpoint),
                    local_role=EndpointType.REQUIRES,
                    remote_model=prov_model,
                    remote_application=prov_app,
                    remote_endpoint=pcmr.provides_endpoint,
                    offer_name=pcmr.offer_name,
                    url=url,
                )
            )

        # Add PROVIDES side to the providing model's bundle
        if prov_model in bundles:
            bundles[prov_model].cross_model_integrations.append(
                CrossModelIntegration(
                    local=ApplicationEndpoint(application=prov_app, endpoint=pcmr.provides_endpoint),
                    local_role=EndpointType.PROVIDES,
                    remote_model=req_model,
                    remote_application=req_app,
                    remote_endpoint=pcmr.requires_endpoint,
                    offer_name=pcmr.offer_name,
                    url=url,
                )
            )

    # Mirror user-specified CMRs to the providing model so export_mermaid can draw edges.
    # Only applies to in-spec CMRs where the remote model is also in this solution.
    for model_name, bundle in list(bundles.items()):
        for cmr in bundle.cross_model_integrations:
            if cmr.local_role != EndpointType.REQUIRES:
                continue
            if cmr.remote_model not in bundles:
                continue
            remote_bundle = bundles[cmr.remote_model]
            # Skip if a PROVIDES entry already exists (e.g. added by PotentialCMR logic).
            already_present = any(
                c.local_role == EndpointType.PROVIDES
                and c.local.application == cmr.remote_application
                and c.local.endpoint == cmr.remote_endpoint
                and c.remote_model == model_name
                and c.remote_application == cmr.local.application
                for c in remote_bundle.cross_model_integrations
            )
            if already_present:
                continue
            remote_app = remote_bundle.applications.get(cmr.remote_application)
            if remote_app is None:
                continue
            remote_bundle.cross_model_integrations.append(
                CrossModelIntegration(
                    local=ApplicationEndpoint(
                        application=cmr.remote_application,
                        endpoint=cmr.remote_endpoint,
                    ),
                    local_role=EndpointType.PROVIDES,
                    remote_model=model_name,
                    remote_application=cmr.local.application,
                    remote_endpoint=cmr.local.endpoint,
                    offer_name=cmr.offer_name,
                    url=cmr.url,
                )
            )

    return Solution(bundles=list(bundles.values()))
