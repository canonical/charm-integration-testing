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

import logging

import z3

from .bundle import Application, ApplicationEndpoint, Bundle
from .problem_space import ProblemSpace


def extract_bundle(model: z3.ModelRef, problem_space: ProblemSpace, logger: logging.Logger) -> Bundle:
    existing_charm_ids = []
    for charm_id, charm in enumerate(problem_space.charms):
        if model.evaluate(charm.exists, model_completion=True):
            existing_charm_ids.append(charm_id)

    charm_id_to_app_name: dict[int, str] = {}
    used_names: set[str] = set()

    for charm_id in existing_charm_ids:
        for (application, mapped_charm_id), mapping_var in problem_space.application_to_charm.items():
            if mapped_charm_id == charm_id and model.evaluate(mapping_var, model_completion=True):
                charm_id_to_app_name[charm_id] = application
                used_names.add(application)
                logger.info(
                    f"Application {application} mapped to charm {problem_space.charms[charm_id].spec.name} (id={charm_id})"
                )
                break

    for charm_id in existing_charm_ids:
        if charm_id in charm_id_to_app_name:
            continue
        base_name = problem_space.charms[charm_id].spec.name
        app_name = base_name
        suffix_ord = ord("a")
        while app_name in used_names:
            app_name = f"{base_name}-{chr(suffix_ord)}"
            suffix_ord += 1
        charm_id_to_app_name[charm_id] = app_name
        used_names.add(app_name)
        logger.info(f"Application {app_name} generated for unmapped charm {base_name} (id={charm_id})")

    applications = {}
    for charm_id, app_name in charm_id_to_app_name.items():
        applications[app_name] = Application(
            charm=problem_space.charms[charm_id].spec,
        )

    integrations = set()
    for integration_key, integration_var in problem_space.charm_integrations.items():
        if model.evaluate(integration_var.exists, model_completion=True):
            charm_ep_1, charm_ep_2 = sorted(integration_key)
            charm_id_1, endpoint_1 = charm_ep_1
            charm_id_2, endpoint_2 = charm_ep_2

            app_name_1 = charm_id_to_app_name.get(charm_id_1)
            app_name_2 = charm_id_to_app_name.get(charm_id_2)

            if app_name_1 and app_name_2:
                integration = frozenset(
                    {
                        ApplicationEndpoint(application=app_name_1, endpoint=endpoint_1),
                        ApplicationEndpoint(application=app_name_2, endpoint=endpoint_2),
                    }
                )
                integrations.add(integration)
                logger.info(f"Integration {app_name_1}:{endpoint_1} -- {app_name_2}:{endpoint_2}")

    return Bundle(
        applications=applications,
        integrations=integrations,
        platform=problem_space.platform_constraint,
        arch=problem_space.arch_constraint,
    )
