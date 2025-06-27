# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


import pytest
from juju import JujuClient


def pytest_addoption(parser):
    parser.addoption(
        "--applications",
        nargs="*",
        type=str,
        required=True,
        default=[],
        help="Applications to remove",
    )


@pytest.fixture
def applications(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--applications")


@pytest.fixture(autouse=True)
def assert_applications_exist(
    assert_idle: None,
    juju_client: JujuClient,
    model: str,
    applications: list[str],
):
    _ = assert_idle # enforce fixture execution order

    for application in applications:
        if not juju_client.application_exists(application, model=model):
            pytest.skip(f"Application {application} not found in model")
