# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


import logging

import pytest
from juju import JujuClient
from juju_cmd import JujuCmdBackend


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger()


@pytest.fixture
def juju_client(logger: logging.Logger) -> JujuClient:
    return JujuClient(JujuCmdBackend(), logger)


def pytest_addoption(parser):
    parser.addoption("--model", type=str, required=True, help="Juju model to test in")


@pytest.fixture
def model(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--model")
