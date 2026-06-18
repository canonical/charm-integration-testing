# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import field
from typing import Any

import yaml
from pydantic import ConfigDict, field_validator
from pydantic.dataclasses import dataclass


@dataclass(config=ConfigDict(frozen=True))
class _OfferSpec:
    """Parsed offer entry from a bundle overlay document."""

    app: str
    endpoints: tuple[str, ...] = ()


@dataclass(config=ConfigDict(frozen=True))
class _AppSpec:
    """Parsed application entry from a bundle base document."""

    charm: str
    channel: str | None = None
    revision: int | None = None
    base: str | None = None
    trust: bool = False
    options: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, str] = field(default_factory=dict)
    scale: int | None = None
    num_units: int | None = None

    @field_validator("options", mode="before")
    @classmethod
    def _drop_none_options(cls, v: Any) -> dict[str, Any]:
        if not v:
            return {}
        if not isinstance(v, dict):
            raise ValueError(f"'options' must be a mapping, got {type(v).__name__}.")
        return {k: val for k, val in v.items() if val is not None}

    @field_validator("resources", mode="before")
    @classmethod
    def _default_resources(cls, v: Any) -> dict[str, str]:
        return v or {}


@dataclass(config=ConfigDict(frozen=True))
class _BundleSpec:
    """Parsed representation of a multi-document Juju bundle YAML."""

    apps: dict[str, _AppSpec] = field(default_factory=dict)
    offers: dict[str, _OfferSpec] = field(default_factory=dict)
    saas: dict[str, str] = field(default_factory=dict)
    relations: tuple[tuple[str, str], ...] = ()


def _parse_bundle_spec(bundle_path: str) -> _BundleSpec:
    """Parse a bundle YAML file (base document + optional overlay) into a _BundleSpec."""
    with open(bundle_path, encoding="utf-8") as f:
        documents = list(yaml.safe_load_all(f))

    if not documents or not isinstance(documents[0], dict):
        raise ValueError(f"Bundle file '{bundle_path}' must contain at least one YAML mapping document.")

    base = documents[0]
    raw_overlay = documents[1] if len(documents) > 1 else None
    if raw_overlay is not None and not isinstance(raw_overlay, dict):
        raise ValueError(
            f"Bundle file '{bundle_path}': second YAML document (overlay) must be a mapping, "
            f"got {type(raw_overlay).__name__}."
        )
    overlay: dict[str, Any] | None = raw_overlay

    apps: dict[str, _AppSpec] = {}
    raw_apps = base.get("applications")
    if raw_apps is not None and not isinstance(raw_apps, dict):
        raise ValueError(
            f"Bundle file '{bundle_path}': 'applications' must be a mapping, got {type(raw_apps).__name__}."
        )
    for app_name, raw in (raw_apps or {}).items():
        if raw is not None and not isinstance(raw, dict):
            raise ValueError(
                f"Bundle file '{bundle_path}': application '{app_name}' entry must be a mapping, "
                f"got {type(raw).__name__}."
            )
        raw = raw or {}
        apps[app_name] = _AppSpec(
            charm=str(raw.get("charm") or app_name),
            channel=raw.get("channel"),
            revision=raw.get("revision"),
            base=raw.get("base"),
            trust=bool(raw.get("trust", False)),
            options=raw.get("options") or {},
            resources=raw.get("resources") or {},
            scale=raw.get("scale"),
            num_units=raw.get("num_units"),
        )

    offers: dict[str, _OfferSpec] = {}
    if overlay:
        raw_overlay_apps = overlay.get("applications")
        if raw_overlay_apps is not None and not isinstance(raw_overlay_apps, dict):
            raise ValueError(
                f"Bundle file '{bundle_path}': overlay 'applications' must be a mapping, "
                f"got {type(raw_overlay_apps).__name__}."
            )
        for app_name, app_data in (raw_overlay_apps or {}).items():
            if app_data is not None and not isinstance(app_data, dict):
                raise ValueError(
                    f"Bundle file '{bundle_path}': overlay application '{app_name}' must be a mapping, "
                    f"got {type(app_data).__name__}."
                )
            raw_offers = (app_data or {}).get("offers")
            if raw_offers is not None and not isinstance(raw_offers, dict):
                raise ValueError(
                    f"Bundle file '{bundle_path}': 'offers' for '{app_name}' must be a mapping, "
                    f"got {type(raw_offers).__name__}."
                )
            for offer_name, offer_config in (raw_offers or {}).items():
                if offer_config is not None and not isinstance(offer_config, dict):
                    raise ValueError(
                        f"Bundle file '{bundle_path}': offer '{offer_name}' config must be a mapping, "
                        f"got {type(offer_config).__name__}."
                    )
                raw_endpoints = (offer_config or {}).get("endpoints")
                if (
                    not raw_endpoints
                    or isinstance(raw_endpoints, str)
                    or not all(isinstance(e, str) for e in raw_endpoints)
                ):
                    raise ValueError(
                        f"Bundle file '{bundle_path}': offer '{offer_name}' endpoints must be a non-empty "
                        f"list of strings, got {raw_endpoints!r}."
                    )
                offers[offer_name] = _OfferSpec(app=app_name, endpoints=tuple(raw_endpoints))

    raw_saas = base.get("saas")
    if raw_saas is not None and not isinstance(raw_saas, dict):
        raise ValueError(f"Bundle file '{bundle_path}': 'saas' must be a mapping, got {type(raw_saas).__name__}.")
    saas: dict[str, str] = {}
    for alias, cfg in (raw_saas or {}).items():
        if not isinstance(cfg, dict):
            raise ValueError(
                f"Bundle file '{bundle_path}': saas entry '{alias}' must be a mapping, " f"got {type(cfg).__name__}."
            )
        url = cfg.get("url")
        if url:
            saas[alias] = url

    raw_relations: list[tuple[str, str]] = []
    for i, rel in enumerate(base.get("relations") or []):
        if not isinstance(rel, list) or len(rel) != 2 or not all(isinstance(e, str) for e in rel):
            raise ValueError(
                f"Bundle file '{bundle_path}': relation at index {i} must be a 2-item list of endpoint strings, "
                f"got {rel!r}."
            )
        raw_relations.append((rel[0], rel[1]))
    relations: tuple[tuple[str, str], ...] = tuple(raw_relations)

    return _BundleSpec(apps=apps, offers=offers, saas=saas, relations=relations)
