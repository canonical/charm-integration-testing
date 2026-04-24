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
    Domain,
    DomainApplication,
    DomainApplicationEndpoint,
    DomainApplicationIntegration,
    DomainModel,
    ModelRef,
)
from .juju_version import JujuVersion
from .snapstore import SnapstoreClient
from .spec import ModelSpec, SpecFile


def classify_integrations(
    model_spec: ModelSpec,
    all_models: dict[str, ModelSpec],
) -> list[DomainApplicationIntegration]:
    """Convert a model's integrations into DomainApplicationIntegrations.

    For local integrations, endpoints carry ``model=None``.
    For cross-model integrations, the remote endpoint carries a ``model`` field,
    and the integration carries ``offer_name`` and ``url``.

    For in-spec CMRs (remote_model is in all_models), the URL is auto-generated
    from the remote model's controller/admin fields.
    """
    result: list[DomainApplicationIntegration] = []

    for integration in model_spec.integrations:
        if not integration.is_cross_model:
            endpoints = [
                (integration.application, integration.endpoint),
                (integration.remote_application, integration.remote_endpoint),
            ]
            sorted_eps = sorted(endpoints, key=lambda e: (e[0], e[1]))
            result.append(
                DomainApplicationIntegration(
                    endpoint_1=DomainApplicationEndpoint(application=sorted_eps[0][0], endpoint=sorted_eps[0][1]),
                    endpoint_2=DomainApplicationEndpoint(application=sorted_eps[1][0], endpoint=sorted_eps[1][1]),
                )
            )
            continue

        remote_model = integration.remote_model
        if remote_model is None:
            raise ValueError("cross-model integration must have a remote_model")
        remote_model_key = integration.remote_model_key or remote_model
        offer_name = integration.resolved_offer_name()

        # Determine URL: explicit for external CMRs, auto-generated for in-spec CMRs
        url = integration.url
        # Resolve the remote ModelRef: the spec may use a plain-name alias, but the
        # domain is keyed by model_spec.key (which may be controller/name).
        # Fall back to remote_model_key when remote_spec.name is None (unit tests that
        # construct ModelSpec directly without going through SpecFile validation).
        remote_ref = ModelRef(name=remote_model_key)
        if remote_model_key in all_models:
            remote_spec = all_models[remote_model_key]
            if remote_spec.name is not None:
                remote_ref = ModelRef(name=remote_spec.name, controller=remote_spec.controller)
            if url is None:
                controller = remote_spec.controller
                admin = remote_spec.admin
                bare_name = remote_spec.name or remote_model_key
                url = f"{controller}:{admin}/{bare_name}.{offer_name}"

        local_ep = DomainApplicationEndpoint(
            application=integration.application,
            endpoint=integration.endpoint,
        )
        remote_ep = DomainApplicationEndpoint(
            application=integration.remote_application,
            endpoint=integration.remote_endpoint,
            model=remote_ref,
        )
        sorted_ep_list = sorted([local_ep, remote_ep], key=lambda e: (e.model.key, e.application, e.endpoint))
        result.append(
            DomainApplicationIntegration(
                endpoint_1=sorted_ep_list[0],
                endpoint_2=sorted_ep_list[1],
                offer_name=offer_name,
                url=url,
            )
        )

    return result


def applications_from_spec(model_spec: ModelSpec) -> dict[str, DomainApplication]:
    """Convert a ModelSpec's applications into DomainApplications."""
    return {
        name: DomainApplication(
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
        domain = Domain()
        for model_spec in spec.models:
            model_ref = ModelRef(name=cast(str, model_spec.name), controller=model_spec.controller)
            applications = applications_from_spec(model_spec)
            integration_constraints = classify_integrations(model_spec, all_models)
            domain.models[model_ref] = DomainModel(
                arch=model_spec.arch,
                platform=model_spec.platform,
                juju_version=self._resolve_juju_version(model_spec.juju),
                ref=model_ref,
                applications=applications,
                application_integrations=integration_constraints,
            )
        return domain

    def _resolve_juju_version(self, juju_str: str) -> JujuVersion:
        if "/" in juju_str:
            resolved = self._snapstore.resolve_snap_version("juju", juju_str)
            self._logger.debug(f"Resolved Juju version {resolved} from channel reference '{juju_str}'")
            return JujuVersion.parse(resolved)
        return JujuVersion.parse(juju_str)
