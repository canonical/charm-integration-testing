# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


import pytest
from juju import JujuClient


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--applications",
        nargs="*",
        type=str,
        required=True,
        default=[],
        help="Applications to redeploy",
    )
    parser.addoption(
        "--bundles",
        nargs="*",
        type=str,
        default=[],
        help="Bundles to deploy",
    )


@pytest.fixture
def applications(request: pytest.FixtureRequest) -> list[str]:
    option = request.config.getoption("--applications")
    assert isinstance(option, list)
    return option


@pytest.fixture
def bundles(request: pytest.FixtureRequest) -> list[str]:
    option = request.config.getoption("--bundles")
    assert isinstance(option, list)
    return option


@pytest.fixture(autouse=True)
def assert_target_application_not_exist(
    juju_client: JujuClient,
    model: str,
    applications: list[str],
) -> None:
    for application in applications:
        if juju_client.application_exists(application, model=model):
            pytest.skip(f"Application {application} already exists in model")
