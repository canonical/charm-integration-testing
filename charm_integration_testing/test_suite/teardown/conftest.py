# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.


import pytest


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
