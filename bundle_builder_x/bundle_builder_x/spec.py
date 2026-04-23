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

"""Spec file models for multi-model bundle building."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml
from pydantic import BaseModel, ConfigDict, model_validator


class AppSpec(BaseModel):
    """Specification for a single application in a model."""

    model_config = ConfigDict(frozen=True)

    charm: str
    channel: str | None = None
    revision: int | None = None
    base: str | None = None


class IntegrationSpec(BaseModel):
    """Specification for a single integration (local or cross-model).

    Local integrations omit ``remote_model``. Cross-model integrations set
    ``remote_model`` to the name of another model in the spec (in-spec CMR)
    or to a model name not present in the spec (external CMR, which requires
    ``url``).
    """

    model_config = ConfigDict(frozen=True)

    application: str
    endpoint: str
    remote_application: str
    remote_endpoint: str
    remote_model: str | None = None
    offer_name: str | None = None
    url: str | None = None

    @property
    def is_cross_model(self) -> bool:
        return self.remote_model is not None

    def resolved_offer_name(self) -> str:
        """Return the offer name, falling back to ``<remote_application>-offer``."""
        if self.offer_name is not None:
            return self.offer_name
        return f"{self.remote_application}-offer"


class ModelSpec(BaseModel):
    """Specification for a single Juju model."""

    model_config = ConfigDict(frozen=True)

    name: str | None = None
    arch: str = "amd64"
    platform: str = "kubernetes"
    juju: str = "3/stable"
    controller: str | None = None
    admin: str = "admin"
    applications: dict[str, AppSpec]
    integrations: list[IntegrationSpec] = []


class SpecFile(BaseModel):
    """Top-level spec file describing one or more models to build."""

    model_config = ConfigDict(frozen=True)

    models: list[ModelSpec]

    @property
    def models_by_name(self) -> dict[str, ModelSpec]:
        """Return a dict mapping model name to ModelSpec."""
        return {m.name: m for m in self.models}  # type: ignore[misc]

    @model_validator(mode="after")
    def _validate_models(self) -> SpecFile:
        # A spec with no models is useless and almost certainly a mistake.
        if not self.models:
            raise ValueError("SpecFile must contain at least one model")

        # Validate names are present and unique
        seen: set[str] = set()
        for i, model_spec in enumerate(self.models):
            if model_spec.name is None:
                raise ValueError(f"Model at index {i} is missing a 'name' field")
            if model_spec.name in seen:
                raise ValueError(f"Duplicate model name: '{model_spec.name}'")
            seen.add(model_spec.name)

            if not model_spec.applications:
                raise ValueError(f"Model '{model_spec.name}' must have at least one application")

        all_models = self.models_by_name
        for model_spec in self.models:
            model_name = model_spec.name
            seen_local: set[tuple[str, str, str, str]] = set()
            seen_cmrs: set[tuple[str, str, str, str, str]] = set()
            for integration in model_spec.integrations:
                if not integration.is_cross_model:
                    # Local integration: both apps must be in this model
                    if integration.application not in model_spec.applications:
                        raise ValueError(
                            f"Model '{model_name}': integration references local application "
                            f"'{integration.application}' which is not defined in this model's applications"
                        )
                    if integration.remote_application not in model_spec.applications:
                        raise ValueError(
                            f"Model '{model_name}': integration references local application "
                            f"'{integration.remote_application}' which is not defined in this model's applications"
                        )
                    # Self-integration: an application cannot integrate with itself on the same endpoint
                    if (
                        integration.application == integration.remote_application
                        and integration.endpoint == integration.remote_endpoint
                    ):
                        raise ValueError(
                            f"Model '{model_name}': application '{integration.application}' cannot "
                            f"integrate with itself on endpoint '{integration.endpoint}'"
                        )
                    # Duplicate local integration: canonical key sorts endpoints so order doesn't matter
                    ep_a = (integration.application, integration.endpoint)
                    ep_b = (integration.remote_application, integration.remote_endpoint)
                    local_key = (*min(ep_a, ep_b), *max(ep_a, ep_b))
                    if local_key in seen_local:
                        raise ValueError(
                            f"Model '{model_name}': duplicate local integration "
                            f"'{integration.application}:{integration.endpoint} -- "
                            f"{integration.remote_application}:{integration.remote_endpoint}'"
                        )
                    seen_local.add(local_key)
                    continue

                remote_model = cast(str, integration.remote_model)  # guaranteed by is_cross_model

                # A CMR whose remote_model is the current model is nonsensical; use a local integration instead.
                if remote_model == model_name:
                    raise ValueError(
                        f"Model '{model_name}': cross-model integration has remote_model equal to the "
                        f"current model; use a local integration instead"
                    )

                # Duplicate CMR: same (local_app, endpoint, remote_model, remote_app, remote_endpoint) twice.
                cmr_key = (
                    integration.application,
                    integration.endpoint,
                    remote_model,
                    integration.remote_application,
                    integration.remote_endpoint,
                )
                if cmr_key in seen_cmrs:
                    raise ValueError(
                        f"Model '{model_name}': duplicate cross-model integration "
                        f"'{integration.application}:{integration.endpoint} -> "
                        f"{remote_model}/{integration.remote_application}:{integration.remote_endpoint}'"
                    )
                seen_cmrs.add(cmr_key)

                # Cross-model: local app must exist in this model
                if integration.application not in model_spec.applications:
                    raise ValueError(
                        f"Model '{model_name}': cross-model integration references local application "
                        f"'{integration.application}' which is not defined in this model's applications"
                    )

                if remote_model in all_models:
                    # In-spec CMR: remote model must have the referenced application
                    remote_model_spec = all_models[remote_model]
                    if integration.remote_application not in remote_model_spec.applications:
                        raise ValueError(
                            f"Model '{model_name}': cross-model integration references application "
                            f"'{integration.remote_application}' in model '{remote_model}', "
                            f"but that application is not defined there"
                        )
                    # In-spec CMR: remote model must have controller set (unless url is provided explicitly)
                    if integration.url is None and remote_model_spec.controller is None:
                        raise ValueError(
                            f"Model '{model_name}': cross-model integration references model "
                            f"'{remote_model}' which has no 'controller' set"
                        )
                else:
                    # External CMR: url is required
                    if integration.url is None:
                        raise ValueError(
                            f"Model '{model_name}': cross-model integration to external model "
                            f"'{remote_model}' requires a 'url' field"
                        )
        return self

    @classmethod
    def load(cls, path: Path) -> SpecFile:
        """Load and validate a spec file from a YAML path."""
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(raw)
