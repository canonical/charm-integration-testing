# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Spec file models for multi-model bundle building."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain import ModelRef


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
    remote_model: ModelRef | None = None
    offer_name: str | None = None
    url: str | None = None

    @field_validator("remote_model", mode="before")
    @classmethod
    def _coerce_remote_model(cls, v: object) -> object:
        """Coerce a string to a ModelRef for convenience in YAML spec files."""
        if isinstance(v, str):
            return ModelRef(name=v)
        return v

    @property
    def is_cross_model(self) -> bool:
        return self.remote_model is not None

    @property
    def remote_model_key(self) -> str | None:
        """Lookup key for the remote model in ``models_by_name``.

        Returns ``controller/name`` when remote model's ``controller`` is set, otherwise
        the plain model name.
        """
        if self.remote_model is None:
            return None
        return self.remote_model.key

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
    integrations: list[IntegrationSpec] = Field(default_factory=list)

    @property
    def key(self) -> str:
        """Unique domain key for this model.

        Returns ``controller/name`` when a controller is set, so that two models
        with the same name on different controllers can coexist in one spec.
        Falls back to plain ``name`` when no controller is set.
        """
        if self.controller is not None:
            return f"{self.controller}/{self.name}"
        return cast(str, self.name)


class SpecFile(BaseModel):
    """Top-level spec file describing one or more models to build."""

    model_config = ConfigDict(frozen=True)

    models: list[ModelSpec]

    @property
    def models_by_name(self) -> dict[str, ModelSpec]:
        """Return a dict mapping model identifiers to ModelSpec.

        Each model is always accessible by its full key (``controller/name`` when a
        controller is set, plain ``name`` otherwise).  When a plain name is
        unambiguous (only one model carries that name), the plain name is also
        included as an alias so that CMR specs do not need to change when models
        gain a controller.

        When two models share the same name on different controllers, only the
        full ``controller/name`` keys resolve to a model; the plain name is
        omitted to avoid ambiguity.
        """
        name_counts: dict[str, int] = {}
        for m in self.models:
            if m.name:
                name_counts[m.name] = name_counts.get(m.name, 0) + 1
        result: dict[str, ModelSpec] = {}
        for m in self.models:
            result[m.key] = m
            # Add plain-name alias only when unambiguous and not already the key
            if m.name and name_counts[m.name] == 1 and m.key != m.name:
                result[m.name] = m
        return result

    @model_validator(mode="after")
    def _validate_models(self) -> SpecFile:
        # A spec with no models is useless and almost certainly a mistake.
        if not self.models:
            raise ValueError("SpecFile must contain at least one model")

        # Validate names are present and unique within each controller
        seen_keys: set[str] = set()
        for i, model_spec in enumerate(self.models):
            if model_spec.name is None:
                raise ValueError(f"Model at index {i} is missing a 'name' field")
            key = model_spec.key
            if key in seen_keys:
                if model_spec.controller is not None:
                    raise ValueError(
                        f"Duplicate model name '{model_spec.name}' on controller '{model_spec.controller}'"
                    )
                else:
                    raise ValueError(
                        f"Duplicate model name: '{model_spec.name}'. "
                        f"Set a 'controller' on each model to allow same-name models on different controllers."
                    )
            seen_keys.add(key)

            if not model_spec.applications:
                raise ValueError(f"Model '{model_spec.name}' must have at least one application")

        all_models = self.models_by_name
        for model_spec in self.models:
            model_name = model_spec.key
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

                remote_model_key = cast(str, integration.remote_model_key)  # includes controller if set

                # A CMR whose remote_model resolves to the current model is nonsensical;
                # use a local integration instead.
                # Compare via resolved key so plain-name aliases and full keys both match.
                resolved_remote = all_models.get(remote_model_key)
                resolved_remote_key = resolved_remote.key if resolved_remote is not None else remote_model_key
                if resolved_remote_key == model_name or remote_model_key == model_name:
                    raise ValueError(
                        f"Model '{model_name}': cross-model integration has remote_model equal to the "
                        f"current model; use a local integration instead"
                    )

                # Duplicate CMR: same (local_app, endpoint, remote_model_key, remote_app, remote_endpoint) twice.
                cmr_key = (
                    integration.application,
                    integration.endpoint,
                    remote_model_key,
                    integration.remote_application,
                    integration.remote_endpoint,
                )
                if cmr_key in seen_cmrs:
                    raise ValueError(
                        f"Model '{model_name}': duplicate cross-model integration "
                        f"'{integration.application}:{integration.endpoint} -> "
                        f"{remote_model_key}/{integration.remote_application}:{integration.remote_endpoint}'"
                    )
                seen_cmrs.add(cmr_key)

                # Cross-model: local app must exist in this model
                if integration.application not in model_spec.applications:
                    raise ValueError(
                        f"Model '{model_name}': cross-model integration references local application "
                        f"'{integration.application}' which is not defined in this model's applications"
                    )

                if remote_model_key in all_models:
                    # In-spec CMR: remote model must have the referenced application
                    remote_model_spec = all_models[remote_model_key]
                    if integration.remote_application not in remote_model_spec.applications:
                        raise ValueError(
                            f"Model '{model_name}': cross-model integration references application "
                            f"'{integration.remote_application}' in model '{remote_model_key}', "
                            f"but that application is not defined there"
                        )
                    # In-spec CMR: remote model must have controller set (unless url is provided explicitly)
                    if integration.url is None and remote_model_spec.controller is None:
                        raise ValueError(
                            f"Model '{model_name}': cross-model integration references model "
                            f"'{remote_model_key}' which has no 'controller' set"
                        )
                else:
                    # External CMR: url is required
                    if integration.url is None:
                        raise ValueError(
                            f"Model '{model_name}': cross-model integration to external model "
                            f"'{remote_model_key}' requires a 'url' field"
                        )
        return self

    @classmethod
    def load(cls, path: str | Path) -> SpecFile:
        """Load and validate a spec file from a YAML path."""
        spec_path = Path(path)
        raw = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        return cls.model_validate(raw)
