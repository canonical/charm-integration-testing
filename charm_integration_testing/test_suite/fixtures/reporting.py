# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Test reporting fixtures: per-test Juju status logging and result hooks.

All fixtures here are loaded via ``pytest_plugins`` in the top-level conftest.
They provide consistent pre/post-test logging of Juju model state and surface
structured failure information from the report hook into the fixture layer.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest
from juju import JujuClient, JujuWaitTimeoutError

from .metadata import (
    error_message,
    failure_exception,
    failure_message,
    skipped_message,
)

# ---------------------------------------------------------------------------
# Exception types that are treated as expected (i.e. "known") test failures.
# Any exception type NOT in this set is classified as an unexpected error.
# ---------------------------------------------------------------------------

KNOWN_FAILURE_EXCEPTIONS = (
    JujuWaitTimeoutError,
    AssertionError,
)


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Iterator[None]:
    """Intercept test reports to stash failure/skip details for downstream fixtures.

    This hook runs *first* (``tryfirst=True``) so that the stash is populated
    before any fixture teardown code tries to read it.
    """
    result = yield
    assert result is not None
    report = result.get_result()

    unexpected_error = False

    if call.excinfo is not None:
        exc_type = call.excinfo.type
        # Ignore pytest's own internal exceptions (skip, xfail, exit).
        if exc_type.__name__ not in ("Skipped", "XFailed", "Exit"):
            if exc_type not in KNOWN_FAILURE_EXCEPTIONS:
                unexpected_error = True

    if report.failed:
        reprcrash = getattr(report.longrepr, "reprcrash", None)
        msg = reprcrash.message if reprcrash is not None else str(report.longrepr)
        item.stash[failure_message] = msg
        if unexpected_error:
            item.stash[error_message] = msg

    if report.skipped:
        if hasattr(report, "wasxfail"):
            reason = report.wasxfail.removeprefix("reason: ")
        elif isinstance(report.longrepr, tuple) and len(report.longrepr) >= 3:
            _, _, reason = report.longrepr
            reason = reason.removeprefix("Skipped: ")
        else:
            reason = str(report.longrepr).removeprefix("Skipped: ")
        item.stash[skipped_message] = reason

    if call.excinfo is not None:
        item.stash[failure_exception] = call.excinfo.value


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def print_setup_and_teardown_info(
    request: pytest.FixtureRequest,
    logger: Any,
    juju_client: JujuClient,
    model: str,
    record_execution_metadata: None,
) -> Iterator[None]:
    """Print Juju status before and after every test, and log start/end events.

    This fixture is *autouse* so that every test gets consistent pre/post
    logging without needing to declare it explicitly.

    The ``record_execution_metadata`` dependency is declared here to enforce
    that metadata recording is set up before this fixture's body runs.
    """
    # Enforce metadata fixture setup first.
    _ = record_execution_metadata

    juju_client.print_status(model=model)
    logger.info("Starting %s", request.node.name)

    yield

    # Log the outcome.
    if error_message in request.node.stash:
        logger.error("Error in %s: %s", request.node.name, request.node.stash[error_message])
    elif failure_message in request.node.stash:
        logger.error("Failure in %s: %s", request.node.name, request.node.stash[failure_message])
    elif skipped_message in request.node.stash:
        logger.info("Skipped %s: %s", request.node.name, request.node.stash[skipped_message])
    else:
        logger.info("Successfully ran %s", request.node.name)

    juju_client.print_status(model=model)
