# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


from datetime import timedelta

import pytest
from juju import JujuClient, JujuWaitTimeoutError


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--bundles",
        nargs="*",
        type=str,
        default=[],
        help="Bundles to deploy",
    )


@pytest.fixture
def bundles(request: pytest.FixtureRequest) -> list[str]:
    option = request.config.getoption("--bundles")
    assert isinstance(option, list)
    return option


@pytest.fixture(autouse=True)
def assert_idle(juju_client: JujuClient, model: str, print_setup_and_teardown_info: None) -> None:
    # Enforce fixture execution order
    _ = print_setup_and_teardown_info

    try:
        juju_client.idle_for_period(model=model, timeout=timedelta(seconds=30), count=5)
    except JujuWaitTimeoutError as e:
        pytest.skip(str(e))
