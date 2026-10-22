# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helpers for lazily accessing Juju fixtures."""

from __future__ import annotations

import pytest
from juju import JujuModelHandle
from resource_registry import ResourceRegistry


def get_registered_model_handles(session_resource_registry: ResourceRegistry) -> list[JujuModelHandle]:
    """Return the currently registered Juju model handles."""
    return [handle for handle in session_resource_registry.registered_handles() if isinstance(handle, JujuModelHandle)]


def needs_juju_client(request: pytest.FixtureRequest, session_resource_registry: ResourceRegistry) -> bool:
    """Whether this test already has models or needs Juju during its test body."""
    return bool(get_registered_model_handles(session_resource_registry)) or "juju_client" in request.fixturenames
