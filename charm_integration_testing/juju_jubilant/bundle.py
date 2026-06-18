# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import field
from typing import Any

import yaml
from pydantic import field_validator
from pydantic.dataclasses import dataclass


@dataclass
class _OfferSpec:
    """Parsed offer entry from a bundle overlay document."""

    app: str
    endpoints: list[str] = field(default_factory=list)


@dataclass
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
        return {k: val for k, val in v.items() if val is not None}

    @field_validator("resources", mode="before")
    @classmethod
    def _default_resources(cls, v: Any) -> dict[str, str]:
        return v or {}


@dataclass
class _BundleSpec:
    """Parsed representation of a multi-document Juju bundle YAML."""

    apps: dict[str, _AppSpec] = field(default_factory=dict)
    offers: dict[str, _OfferSpec] = field(default_factory=dict)
    saas: dict[str, str] = field(default_factory=dict)
    relations: list[tuple[str, str]] = field(default_factory=list)


def _parse_bundle_spec(bundle_path: str) -> _BundleSpec:
    """Parse a bundle YAML file (base document + optional overlay) into a _BundleSpec."""
    with open(bundle_path, encoding="utf-8") as f:
        documents = list(yaml.safe_load_all(f))

    if not documents or not isinstance(documents[0], dict):
        raise ValueError(f"Bundle file '{bundle_path}' must contain at least one YAML mapping document.")

    base = documents[0]
    overlay = documents[1] if len(documents) > 1 else None

    apps: dict[str, _AppSpec] = {}
    for app_name, raw in (base.get("applications") or {}).items():
        raw = raw or {}
        apps[app_name] = _AppSpec(
            charm=raw.get("charm", app_name),
            channel=raw.get("channel"),
            revision=raw.get("revision"),
            base=raw.get("base"),
            trust=bool(raw.get("trust", False)),
            options=raw.get("options"),
            resources=raw.get("resources"),
            scale=raw.get("scale"),
            num_units=raw.get("num_units"),
        )

    offers: dict[str, _OfferSpec] = {}
    if overlay:
        for app_name, app_data in (overlay.get("applications") or {}).items():
            for offer_name, offer_config in ((app_data or {}).get("offers") or {}).items():
                offers[offer_name] = _OfferSpec(
                    app=app_name,
                    endpoints=(offer_config or {}).get("endpoints") or [],
                )

    saas: dict[str, str] = {
        alias: cfg["url"] for alias, cfg in (base.get("saas") or {}).items() if cfg and cfg.get("url")
    }

    relations: list[tuple[str, str]] = []
    for rel in base.get("relations") or []:
        endpoints = [r[0] if isinstance(r, list) else r for r in rel]
        if len(endpoints) == 2:
            relations.append((endpoints[0], endpoints[1]))

    return _BundleSpec(apps=apps, offers=offers, saas=saas, relations=relations)
