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

"""Converts a SpecFile into a Z3 Domain.

This module bridges the spec (user input) and domain (solver) layers,
owning the translation from spec types to domain types, including Juju
version resolution via the Snapstore API.
"""

import logging
from typing import cast

from .domain import (
    ApplicationConstraint,
    CrossModelIntegrationConstraint,
    CrossModelRemote,
    Domain,
    IntegrationConstraint,
    ModelInit,
    initialize_global_domain,
)
from .juju_version import JujuVersion
from .snapstore import SnapstoreClient
from .spec import ModelSpec, SpecFile


def classify_integrations(
    model_name: str,
    model_spec: ModelSpec,
    all_models: dict[str, ModelSpec],
) -> tuple[set[IntegrationConstraint], list[CrossModelIntegrationConstraint]]:
    """Split a model's integrations into local and cross-model constraints.

    For in-spec CMRs (remote_model is in all_models), the URL is auto-generated
    from the remote model's controller/admin fields.

    Returns:
        A tuple of (local IntegrationConstraints, cross-model constraints).
    """
    local: set[IntegrationConstraint] = set()
    cross_model: list[CrossModelIntegrationConstraint] = []

    for integration in model_spec.integrations:
        if not integration.is_cross_model:
            local.add(
                IntegrationConstraint(
                    application_1=integration.application,
                    endpoint_1=integration.endpoint,
                    application_2=integration.remote_application,
                    endpoint_2=integration.remote_endpoint,
                )
            )
            continue

        remote_model = integration.remote_model
        if remote_model is None:
            raise ValueError("cross-model integration must have a remote_model")
        offer_name = integration.resolved_offer_name()

        # Determine URL: explicit for external CMRs, auto-generated for in-spec CMRs
        url = integration.url
        if url is None and remote_model in all_models:
            remote_spec = all_models[remote_model]
            controller = remote_spec.controller
            admin = remote_spec.admin
            url = f"{controller}:{admin}/{remote_model}.{offer_name}"

        cross_model.append(
            CrossModelIntegrationConstraint(
                local_application=integration.application,
                local_endpoint=integration.endpoint,
                remote=CrossModelRemote(
                    model=remote_model,
                    application=integration.remote_application,
                    endpoint=integration.remote_endpoint,
                    offer_name=offer_name,
                    url=url,
                ),
            )
        )

    return local, cross_model


def applications_from_spec(model_spec: ModelSpec) -> dict[str, ApplicationConstraint]:
    """Convert a ModelSpec's applications into ApplicationConstraints."""
    return {
        name: ApplicationConstraint(
            charm=app.charm,
            channel=app.channel,
            revision=app.revision,
            base=app.base,
        )
        for name, app in model_spec.applications.items()
    }


class DomainBuilder:
    """Builds a Z3 Domain from a SpecFile.

    Owns the full spec-to-domain translation, including Juju version resolution
    via the Snapstore API.
    """

    def __init__(
        self,
        snapstore_client: SnapstoreClient,
        logger: logging.Logger = logging.getLogger(__name__),
    ):
        self._snapstore = snapstore_client
        self._logger = logger

    def build(self, spec: SpecFile) -> Domain:
        """Convert a spec file into an initialized Z3 domain."""
        all_models = spec.models_by_name
        model_inits: dict[str, ModelInit] = {}
        for model_spec in spec.models:
            model_name = cast(str, model_spec.name)
            local_integrations, cross_model = classify_integrations(model_name, model_spec, all_models)
            model_inits[model_name] = ModelInit(
                applications=applications_from_spec(model_spec),
                integrations=local_integrations,
                platform=model_spec.platform,
                arch=model_spec.arch,
                juju_version=self._resolve_juju_version(model_spec.juju),
                cross_model_integrations=cross_model,
                controller=model_spec.controller,
            )
        return initialize_global_domain(model_inits)

    def _resolve_juju_version(self, juju_str: str) -> JujuVersion:
        if "/" in juju_str:
            resolved = self._snapstore.resolve_snap_version("juju", juju_str)
            self._logger.debug(f"Resolved Juju version {resolved} from channel reference '{juju_str}'")
            return JujuVersion.parse(resolved)
        return JujuVersion.parse(juju_str)
