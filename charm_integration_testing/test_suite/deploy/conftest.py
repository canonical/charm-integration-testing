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
    result: list[tuple[tuple[str, str], tuple[str, str]]] = []
    for integration in request.config.getoption("--integrations"):
        targets = integration.split("/", 1)
        assert len(targets) == 2
        first = targets[0].split(":", 1)
        second = targets[1].split(":", 1)
        assert len(first) == 2
        assert len(second) == 2
        result.append((tuple(first), tuple(second)))
    return result
