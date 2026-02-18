# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import pytest
from juju import JujuClient, JujuWaitTimeoutError


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--applications",
        nargs="*",
        type=str,
        required=True,
        default=[],
        help="Applications to remove",
    )


@pytest.fixture
def applications(request: pytest.FixtureRequest) -> list[str]:
    option = request.config.getoption("--applications")
    assert isinstance(option, list)
    return option


@pytest.fixture(autouse=True)
def assert_applications_exist(
    assert_idle: None,
    juju_client: JujuClient,
    model: str,
    applications: list[str],
) -> None:
    _ = assert_idle  # Enforce fixture execution order

    for application in applications:
        if not juju_client.application_exists(application, model=model):
            pytest.skip(f"Application {application} not found in model")


@pytest.fixture(autouse=True)
def assert_idle(juju_client: JujuClient, model: str, print_setup_and_teardown_info: None) -> None:
    # Enforce fixture execution order
    _ = print_setup_and_teardown_info

    try:
        juju_client.idle_for_period(model=model, timeout=timedelta(seconds=30), count=5)
    except JujuWaitTimeoutError as e:
        pytest.skip(str(e))
