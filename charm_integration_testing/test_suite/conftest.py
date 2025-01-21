# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


import pytest

from charm_integration_testing.juju import JujuClient
from charm_integration_testing.juju_cmd import JujuCmdBackend

import logging

@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger(__file__)


@pytest.fixture
def juju_client(logger: logging.Logger) -> JujuClient:
    return JujuClient(JujuCmdBackend(), logger)


def pytest_addoption(parser):
    parser.addoption("--model", type=str, required=True, help="Juju model that contains integration to test")
    parser.addoption(
        "--requirer",
        type=str,
        required=True,
        help="Application endpoint under test, formatted as <application:endpoint>",
    )
    parser.addoption(
        "--provider",
        type=str,
        required=True,
        help="Neighbor endpoint to integrate with, formatted as <application:endpoint>",
    )


@pytest.fixture
def model(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--model")


@pytest.fixture
def requirer_application(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--requirer").split(":", 1)[0]


@pytest.fixture
def requirer_endpoint(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--requirer").split(":", 1)[1]


@pytest.fixture
def provider_application(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--provider").split(":", 1)[0]


@pytest.fixture
def provider_endpoint(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--provider").split(":", 1)[1]


@pytest.fixture(autouse=True)
def assert_idle(juju_client, model: str):
    juju_client.wait_idle(model=model)
