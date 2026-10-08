# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest

from bundle_builder_x import Charm


def require_principal_charm(charm: Charm | None) -> Charm:
    if charm is None:
        pytest.fail("Unable to resolve the deployed target charm metadata needed for HA operations.")
    if charm.subordinate:
        pytest.skip(f"{charm.name} is subordinate and cannot be used for HA operations.")
    return charm
