# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import MagicMock

from juju import JujuClient
from test_suite.fixtures.juju import JujuClientRegistry


def test_juju_client_registry_exposes_registered_client() -> None:
    registry = JujuClientRegistry()
    client = MagicMock(spec=JujuClient)

    assert registry.client is None

    registry.register(client)

    assert registry.client is client
