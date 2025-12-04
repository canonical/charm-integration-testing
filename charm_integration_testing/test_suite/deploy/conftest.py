# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--bundles",
        nargs="*",
        type=str,
        default=[],
        help="Bundles to deploy",
    )
    parser.addoption(
        "--integrations",
        nargs="*",
        type=str,
        default=[],
        help="Additional integrations to deploy, as <application_1>:<endpoint_1>/<application_1>:<application_2>",
    )


@pytest.fixture
def bundles(request: pytest.FixtureRequest) -> list[str]:
    option = request.config.getoption("--bundles")
    assert isinstance(option, list)
    return option


@pytest.fixture
def integrations(request: pytest.FixtureRequest) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    return [
        tuple([tuple(target.split(":", 1)) for target in integration.split("/", 1)])
        for integration in request.config.getoption("--integrations")
    ]
