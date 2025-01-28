# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


import pytest

from charm_integration_testing.juju import JujuClient

from datetime import timedelta


def pytest_addoption(parser):
    parser.addoption(
        "--target-application",
        type=str,
        required=True,
        help="Application under test",
    )
    parser.addoption(
        "--target-endpoint",
        type=str,
        required=True,
        help="Application endpoint under test",
    )
    parser.addoption(
        "--neighbor-application",
        type=str,
        required=True,
        help="Neighbor application to integrate with",
    )
    parser.addoption(
        "--neighbor-endpoint",
        type=str,
        required=True,
        help="Neighbor endpoint to integrate with",
    )


@pytest.fixture
def target_application(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--target-application")


@pytest.fixture
def target_endpoint(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--target-endpoint")


@pytest.fixture
def neighbor_application(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--neighbor-application")


@pytest.fixture
def neighbor_endpoint(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--neighbor-endpoint")


@pytest.fixture(autouse=True)
def assert_idle(juju_client: JujuClient, model: str):
    juju_client.idle_for_period(model=model, timeout=timedelta(seconds=20))
