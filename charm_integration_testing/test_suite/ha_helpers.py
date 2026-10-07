# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

import pytest
import yaml

from bundle_builder_x import Charm


def require_principal_charm(charm: Charm | None) -> Charm:
    if charm is None:
        pytest.fail("Unable to resolve the deployed target charm metadata needed for HA tests.")
    if charm.subordinate:
        pytest.skip(f"{charm.name} is subordinate and cannot be scaled independently.")
    return charm


def bundle_application_units(bundle_path: Path, application: str, platform: str) -> int:
    with bundle_path.open(encoding="utf-8") as file:
        try:
            bundle = next(yaml.safe_load_all(file))
        except StopIteration:
            raise ValueError(f"Bundle is empty: {bundle_path}") from None

    if not isinstance(bundle, dict):
        raise ValueError(f"Invalid bundle document in {bundle_path}.")
    applications = bundle.get("applications")
    if not isinstance(applications, dict) or application not in applications:
        raise ValueError(f"Application '{application}' not found in bundle: {bundle_path}")
    application_data = applications[application]
    if not isinstance(application_data, dict):
        raise ValueError(f"Invalid application definition for '{application}' in {bundle_path}.")

    units_key = "scale" if platform == "kubernetes" else "num_units"
    units = application_data.get(units_key)
    if isinstance(units, bool) or not isinstance(units, int) or units < 1:
        raise ValueError(f"Application '{application}' in {bundle_path} must define a positive integer '{units_key}'.")
    return units
