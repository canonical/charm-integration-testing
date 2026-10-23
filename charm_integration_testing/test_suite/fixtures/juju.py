# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helpers for lazily accessing Juju fixtures."""

from __future__ import annotations

from juju import JujuClient, JujuModelHandle
from resource_registry import ResourceRegistry


class JujuClientRegistry:
    """Expose the session Juju client to test-lifecycle observers."""

    def __init__(self) -> None:
        self._client: JujuClient | None = None

    def register(self, client: JujuClient) -> None:
        self._client = client

    @property
    def client(self) -> JujuClient | None:
        return self._client


def get_registered_model_handles(session_resource_registry: ResourceRegistry) -> list[JujuModelHandle]:
    """Return the currently registered Juju model handles."""
    return [handle for handle in session_resource_registry.registered_handles() if isinstance(handle, JujuModelHandle)]
