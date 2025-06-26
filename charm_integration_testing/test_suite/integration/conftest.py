# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


import pytest
from juju import JujuClient


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
def assert_applications_exist(
    assert_idle: None,
    juju_client: JujuClient,
    model: str,
    target_application: str,
    neighbor_application: str,
):
    if not juju_client.application_exists(target_application, model=model):
        pytest.skip(f"Application {target_application} not found in model")
    if not juju_client.application_exists(neighbor_application, model=model):
        pytest.skip(f"Application {neighbor_application} not found in model")


@pytest.fixture(autouse=True)
def assert_applications_integrated(
    assert_applications_exist: None,
    juju_client: JujuClient,
    model: str,
    target_application: str,
    target_endpoint: str,
    neighbor_application: str,
    neighbor_endpoint: str,
):
    if not juju_client.integration_exists(
        target_application, target_endpoint, neighbor_application, neighbor_endpoint, model=model
    ):
        pytest.skip(f"Integration {target_application}:{target_endpoint} <-> {neighbor_application}:{neighbor_endpoint} not found in model")
