# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helpers for lazily accessing Juju fixtures."""

from __future__ import annotations

import pytest
from juju import JujuClient, JujuModelHandle
from resource_registry import ResourceRegistry


def get_juju_client_for_registered_models(
    request: pytest.FixtureRequest, session_resource_registry: ResourceRegistry
) -> tuple[JujuClient, list[JujuModelHandle]] | None:
    """Return the Juju client and current registered models, if any exist."""
    model_handles = [
        handle for handle in session_resource_registry.registered_handles() if isinstance(handle, JujuModelHandle)
    ]
    if not model_handles:
        return None
    juju_client: JujuClient = request.getfixturevalue("juju_client")
    return juju_client, model_handles
