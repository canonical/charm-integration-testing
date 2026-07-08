# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Utilities for parsing and transforming Juju bundle YAML files."""

from dataclasses import dataclass

import yaml


@dataclass(frozen=True)
class OfferDetails:
    """Details of a single offer declared in a bundle overlay."""

    app: str
    endpoints: list[str]


def strip_saas_from_bundle(bundle_yaml: str) -> str:
    """Return bundle YAML with the saas section removed and cross-model relations filtered out.

    This is used in the first deployment phase so that all models' applications and offers are
    created before any model attempts to consume a remote offer. Without this, bidirectional CMR
    (where both models consume from each other) would deadlock: neither can be deployed first.

    Handles multi-document YAML (base bundle + overlay) produced when the bundle contains offers.

    Raises:
        ValueError: if *bundle_yaml* does not parse to at least one YAML mapping document.
    """
    documents = list(yaml.safe_load_all(bundle_yaml))
    if not documents or not isinstance(documents[0], dict):
        raise ValueError("bundle_yaml must contain at least one YAML mapping document as the base bundle")
    base = documents[0]
    saas_names = set(base.pop("saas", {}).keys())
    if saas_names:
        # Cross-model relations reference the saas alias on one side; filter those out.
        base["relations"] = [
            rel for rel in base.get("relations", []) if not any(ep.split(":")[0] in saas_names for ep in rel)
        ]
    parts = [yaml.dump(base, default_flow_style=False, sort_keys=True)]
    # Re-serialize overlay documents (key order and formatting may change, but content is preserved).
    for doc in documents[1:]:
        if not isinstance(doc, dict):
            continue
        parts.append(yaml.dump(doc, default_flow_style=False, sort_keys=True))
    return "---\n" + "---\n".join(parts) if len(parts) > 1 else parts[0]


def parse_offer_names_from_bundle(bundle_yaml: str) -> set[str]:
    """Return the set of offer names defined in the bundle's overlay documents.

    Offers are declared in overlay documents (documents after the first) under
    ``applications.<app>.offers``. This is used to identify which offers must be created
    before phase 2 on Juju 4+, where updating an existing offer is not supported.
    """
    return set(parse_offers_from_bundle(bundle_yaml).keys())


def parse_offers_from_bundle(bundle_yaml: str) -> dict[str, OfferDetails]:
    """Return offer details from the bundle's overlay documents.

    Returns a dict mapping offer name to an :class:`OfferDetails` instance.
    Offers are declared in overlay documents under ``applications.<app>.offers``.
    Non-dict overlay documents and non-dict application entries are silently skipped.
    """
    documents = list(yaml.safe_load_all(bundle_yaml))
    offers: dict[str, OfferDetails] = {}
    for doc in documents[1:]:
        if not isinstance(doc, dict):
            continue
        for app_name, app_data in doc.get("applications", {}).items():
            if not isinstance(app_data, dict):
                continue
            for offer_name, offer_data in app_data.get("offers", {}).items():
                offers[offer_name] = OfferDetails(
                    app=app_name,
                    endpoints=list(offer_data.get("endpoints", [])),
                )
    return offers


def strip_offers_from_bundle(bundle_yaml: str) -> str:
    """Return bundle YAML with all offer definitions removed from overlay documents.

    This is used for the second deployment phase on Juju 4+, where ``juju offer`` is not
    idempotent and fails if the offer already exists. The offers were already created in phase 1
    so there is no need to re-declare them; stripping them lets the phase-2 deploy proceed
    without touching the existing offers while still establishing the cross-model relations via
    the saas sections in the base bundle.

    Overlay documents that become empty after stripping are dropped.

    Raises:
        ValueError: if *bundle_yaml* does not parse to at least one YAML mapping document.
    """
    documents = list(yaml.safe_load_all(bundle_yaml))
    if not documents or not isinstance(documents[0], dict):
        raise ValueError("bundle_yaml must contain at least one YAML mapping document as the base bundle")
    base = documents[0]
    parts = [yaml.dump(base, default_flow_style=False, sort_keys=True)]
    for doc in documents[1:]:
        if not isinstance(doc, dict):
            continue
        for app_data in doc.get("applications", {}).values():
            if isinstance(app_data, dict):
                app_data.pop("offers", None)
        # Drop the overlay entirely if it is now empty (no remaining keys).
        has_content = any(bool(app_data) for app_data in doc.get("applications", {}).values()) or any(
            k != "applications" for k in doc
        )
        if has_content:
            parts.append(yaml.dump(doc, default_flow_style=False, sort_keys=True))
    return "---\n" + "---\n".join(parts) if len(parts) > 1 else parts[0]
