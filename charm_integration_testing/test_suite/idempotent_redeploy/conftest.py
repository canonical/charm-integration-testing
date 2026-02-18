# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.


import pytest


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
