# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


import logging

import pytest
from juju import JujuClient
from juju_cmd import JujuCmdBackend
from pytest import CollectReport, StashKey


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


failure_message = StashKey[CollectReport]()


# Get failure message for logging
@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    result = yield
    report = result.get_result()
    if report.failed:
        reprcrash = getattr(report.longrepr, "reprcrash", None)
        if reprcrash is not None:
            item.stash[failure_message] = reprcrash.message
        else:
            item.stash[failure_message] = str(report.longrepr)


@pytest.fixture(autouse=True)
def print_setup_and_teardown_info(
    request: pytest.FixtureRequest, logger: logging.Logger, juju_client: JujuClient, model: str
):
    # Print starting state
    juju_client.print_status(model=model)

    # Log starting
    logger.info(f"Starting {request.node.name}")

    yield

    # Log error
    if failure_message in request.node.stash:
        logger.error(f"Failure in {request.node.name}: {request.node.stash[failure_message]}")
    else:
        logger.info(f"Successfully ran {request.node.name}")

    # Log ending state
    juju_client.print_status(model=model)
