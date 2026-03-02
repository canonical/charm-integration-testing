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

import z3

from .bundle import Application, ApplicationEndpoint, Bundle, Integration
from .domain import Domain


def extract_bundle(model: z3.ModelRef, domain: Domain, logger: logging.Logger) -> Bundle:
    existing_charm_ids = []
    for charm_id, charm in enumerate(domain.charms):
        if model.evaluate(charm.exists, model_completion=True):
            existing_charm_ids.append(charm_id)

    charm_id_to_app_name: dict[int, str] = {}
    used_names: set[str] = set()

    for charm_id in existing_charm_ids:
        for mapping, mapping_var in domain.application_to_charm.items():
            if mapping.charm_id == charm_id and model.evaluate(mapping_var, model_completion=True):
                charm_id_to_app_name[charm_id] = mapping.application
                used_names.add(mapping.application)
                logger.info(
                    f"Application {mapping.application} mapped to charm {domain.charms[charm_id].spec.name} (id={charm_id})"
                )
                break

    for charm_id in existing_charm_ids:
        if charm_id in charm_id_to_app_name:
            continue
        base_name = domain.charms[charm_id].spec.name
        app_name = base_name
        suffix_ord = ord("a")
        while app_name in used_names:
            app_name = f"{base_name}-{chr(suffix_ord)}"
            suffix_ord += 1
        charm_id_to_app_name[charm_id] = app_name
        used_names.add(app_name)

    applications = {}
    for charm_id, app_name in charm_id_to_app_name.items():
        charm = domain.charms[charm_id]

        # Extract config from model
        config_index = model.evaluate(charm.config_index, model_completion=True).as_long()
        selected_config = charm.spec.configs[config_index]

        # Filter out None values to get only explicitly set config
        config = {key: value for key, value in selected_config.items() if value is not None}

        applications[app_name] = Application(
            charm=charm.spec,
            config=config,
        )

    integrations = set()
    for charm_integration, integration_var in domain.charm_integrations.items():
        if model.evaluate(integration_var.exists, model_completion=True):
            # Extract endpoints from CharmIntegration model
            charm_id_1 = charm_integration.requires_endpoint.charm_id
            endpoint_1 = charm_integration.requires_endpoint.endpoint
            charm_id_2 = charm_integration.provides_endpoint.charm_id
            endpoint_2 = charm_integration.provides_endpoint.endpoint

            app_name_1 = charm_id_to_app_name.get(charm_id_1)
            app_name_2 = charm_id_to_app_name.get(charm_id_2)

            if app_name_1 and app_name_2:
                integrations.add(
                    Integration(
                        {
                            ApplicationEndpoint(application=app_name_1, endpoint=endpoint_1),
                            ApplicationEndpoint(application=app_name_2, endpoint=endpoint_2),
                        }
                    )
                )

    return Bundle(
        applications=applications,
        integrations=integrations,
        platform=domain.platform_constraint,
        arch=domain.arch_constraint,
    )
