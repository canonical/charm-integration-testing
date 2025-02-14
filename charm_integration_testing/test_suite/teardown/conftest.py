# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

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
def assert_idle(juju_client: JujuClient, model: str):
    juju_client.idle_for_period(model=model, timeout=timedelta(seconds=20))


@pytest.fixture(autouse=True)
def assert_applications_exist(
    assert_idle: None,
    juju_client: JujuClient,
    model: str,
    applications: list[str],
):
    for application in applications:
        assert juju_client.application_exists(application, model=model)
