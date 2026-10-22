# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import MagicMock

import pytest
from juju import JujuModelHandle
from test_suite.fixtures.juju import needs_juju_client


def test_does_not_need_juju_client_without_models_or_test_dependency() -> None:
    request = MagicMock(spec=pytest.FixtureRequest)
    request.fixturenames = []
    session_resource_registry = MagicMock()
    session_resource_registry.registered_handles.return_value = []

    assert not needs_juju_client(request, session_resource_registry)


def test_needs_juju_client_when_a_model_is_registered() -> None:
    request = MagicMock(spec=pytest.FixtureRequest)
    model = JujuModelHandle(model="my-model", controller="my-controller")
    session_resource_registry = MagicMock()
    session_resource_registry.registered_handles.return_value = [model]

    assert needs_juju_client(request, session_resource_registry)


def test_needs_juju_client_when_test_depends_on_it() -> None:
    request = MagicMock(spec=pytest.FixtureRequest)
    request.fixturenames = ["juju_client"]
    session_resource_registry = MagicMock()
    session_resource_registry.registered_handles.return_value = []

    assert needs_juju_client(request, session_resource_registry)
