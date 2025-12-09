# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


import json
import logging
import random
import re
import warnings
from datetime import timedelta
from pathlib import Path
from subprocess import CalledProcessError  # nosec
from typing import Any, Callable

import pytest
from extensions import (
    PostgresqlDatabaseReplicationExtension,
    PostgresqlK8sDatabaseReplicationExtension,
    S3IntegratorMinIOBackendExtension,
    UnsealVaultJujuExtension,
    UnsealVaultK8sJujuExtension,
)
from juju import JujuBackend, JujuClient, JujuWaitTimeoutError
from juju_jubilant import JubilantBackend
from pytest import CollectReport, StashKey


@pytest.fixture
def logger() -> logging.Logger:
    jubilant_logger = logging.getLogger("jubilant")
    jubilant_logger.setLevel(logging.WARNING)

    jubilant_logger_wait = logging.getLogger("jubilant.wait")
    jubilant_logger_wait.setLevel(logging.WARNING)

    return logging.getLogger()


@pytest.fixture
def juju_backend() -> JujuBackend:
    return JubilantBackend()


@pytest.fixture
def juju_client(juju_backend: JujuBackend, logger: logging.Logger, minio_client_file: Path | None) -> JujuClient:
    return JujuClient(
        juju_backend,
        logger,
        extensions=[
            UnsealVaultJujuExtension(juju_backend, logger),
            UnsealVaultK8sJujuExtension(juju_backend, logger),
            S3IntegratorMinIOBackendExtension(juju_backend, logger, minio_client_file),
            PostgresqlDatabaseReplicationExtension(juju_backend, logger),
            PostgresqlK8sDatabaseReplicationExtension(juju_backend, logger),
        ],
    )


def pytest_addoption(parser):
    parser.addoption("--model", type=str, required=True, help="Juju model to test in")
    parser.addoption(
        "--minio-client-file",
        type=Path,
        help="MinIO client file used to create a bucket (for s3-integrator)",
        default=None,
    )


@pytest.fixture
def model(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--model")


@pytest.fixture
def minio_client_file(request: pytest.FixtureRequest) -> Path | None:
    return request.config.getoption("--minio-client-file")


failure_message = StashKey[CollectReport]()
skipped_message = StashKey[CollectReport]()
failure_exception = StashKey[CollectReport]()


# Get failure message for logging
@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    result = yield
    report = result.get_result()

    # Save failure message
    if report.failed:
        # Adapted from https://docs.pytest.org/en/stable/_modules/_pytest/junitxml.html
        reprcrash = getattr(report.longrepr, "reprcrash", None)
        if reprcrash is not None:
            item.stash[failure_message] = reprcrash.message
        else:
            item.stash[failure_message] = str(report.longrepr)

    # Save skip message
    if report.skipped:
        # Adapted from https://docs.pytest.org/en/stable/_modules/_pytest/junitxml.html
        if hasattr(report, "wasxfail"):
            item.stash[skipped_message] = report.wasxfail.removeprefix("reason: ")
        else:
            if isinstance(report.longrepr, tuple) and len(report.longrepr) >= 3:
                _, _, skipreason = report.longrepr
            else:
                skipreason = str(report.longrepr)
            item.stash[skipped_message] = skipreason.removeprefix("Skipped: ")

    # Save failure exception
    if call.excinfo:
        item.stash[failure_exception] = call.excinfo.value


@pytest.fixture(autouse=True)
def print_setup_and_teardown_info(
    request: pytest.FixtureRequest,
    logger: logging.Logger,
    juju_client: JujuClient,
    model: str,
    record_execution_metadata: None,
):
    # Enforce fixture execution order
    _ = record_execution_metadata

    # Print starting state
    juju_client.print_status(model=model)

    # Log starting
    logger.info(f"Starting {request.node.name}")

    yield

    # Log error
    if failure_message in request.node.stash:
        logger.error(f"Failure in {request.node.name}: {request.node.stash[failure_message]}")
    elif skipped_message in request.node.stash:
        logger.info(f"Skipped {request.node.name}: {request.node.stash[skipped_message]}")
    else:
        logger.info(f"Successfully ran {request.node.name}")

    # Log ending state
    juju_client.print_status(model=model)


@pytest.fixture(autouse=True)
def assert_idle(juju_client: JujuClient, model: str, print_setup_and_teardown_info: None):
    # Enforce fixture execution order
    _ = print_setup_and_teardown_info

    try:
        juju_client.idle_for_period(model=model, timeout=timedelta(seconds=15), count=5)
    except JujuWaitTimeoutError:
        pytest.skip("Model is not idle before test start")


@pytest.fixture
def execution_metadata(record_property: Callable[[str, object], None]):
    # Create a function for adding and deduplicating metadata
    metadata: dict[str, set[str]] = {}

    def add(category: str, value: str):
        if category not in metadata:
            metadata[category] = set()
        metadata[category].add(value)

    # Provide the function
    yield add

    # After the test, record all the metadata
    for category, values in metadata.items():
        # JUnit properties are key value, where one key can only be mapped to one value
        # Execution metadata is category value, where one category can be mapped to multiple values
        # So just store the values as a list in the single key
        # and use JSON to ensure characters are escaped properly
        record_property(category, json.dumps([str(value) for value in sorted(values)]))


@pytest.fixture(autouse=True)
def record_execution_metadata(
    record_warning_execution_metadata: None,
    record_failure_execution_metadata: None,
    record_charms_and_revisions_execution_metadata: None,
):
    # Save various execution metadata
    _ = record_warning_execution_metadata
    _ = record_failure_execution_metadata
    _ = record_charms_and_revisions_execution_metadata


@pytest.fixture
def record_warning_execution_metadata(execution_metadata: Callable[[str, str | int], None]):
    # Captured all warnings
    # Pytest normally captures warnings, but does not expose them until after the test report is made
    with warnings.catch_warnings(record=True) as warnings_list:
        # Let the test run
        yield

        # Save all warnings
        for warning in warnings_list:
            execution_metadata("testing", random.randint(1, 100000))
            execution_metadata("warning:message", normalize_message(f"{warning.category.__name__}: {warning.message}"))

    # Re-emit all warnings so they show up in the test summary
    for warning in warnings_list:
        warnings.warn_explicit(
            message=warning.message,
            category=warning.category,
            filename=warning.filename,
            lineno=warning.lineno,
            source=warning.source,
        )


def record_charms_and_revisions_execution_metadata_instantaneous(
    juju_client: JujuClient, model: str, execution_metadata: Callable[[str, str | int], None]
):
    # Get all charm revisions
    for charm, revision in juju_client.get_charm_revisions(model=model):
        # Save the charm
        execution_metadata("charm", charm)
        # Save the revision
        execution_metadata(f"charm:{charm}:revision", revision)


@pytest.fixture
def record_charms_and_revisions_execution_metadata(
    juju_client: JujuClient, model: str, execution_metadata: Callable[[str, str | int], None]
):
    # Save all charms and revisions at start of test
    record_charms_and_revisions_execution_metadata_instantaneous(juju_client, model, execution_metadata)

    # Let the test run
    yield

    # Save all charms and revisions at end of test
    record_charms_and_revisions_execution_metadata_instantaneous(juju_client, model, execution_metadata)


def normalize_message(message: Any) -> str:
    # Convert to string if needed
    if isinstance(message, bytes):
        message = message.decode("utf-8", errors="replace")
    else:
        message = str(message)

    # Replace all numeric sequences with "XXX"
    # Should normalize timestamps, IP addresses, and other variable data
    message = re.sub(r"\d+", "XXX", message)

    # Limit character count
    max_character_count = 150
    if len(message) > max_character_count:
        message = f"{message[:max_character_count - 3]}..."

    return message


@pytest.fixture
def record_failure_execution_metadata(
    request: pytest.FixtureRequest, execution_metadata: Callable[[str, str | int], None]
):
    # Let the test run
    yield

    # Save the failure message
    if failure_message in request.node.stash:
        execution_metadata("failure:message", normalize_message(request.node.stash[failure_message]))

    # Save the skip message
    if skipped_message in request.node.stash:
        execution_metadata("skipped:message", normalize_message(request.node.stash[skipped_message]))

    # Save extra metadata from exception
    if failure_exception in request.node.stash:
        exc = request.node.stash[failure_exception]

        # Save state from wait timeout
        if isinstance(exc, JujuWaitTimeoutError):
            for application in exc.wait_state.noncompliant_applications.values():
                if application is None:
                    continue
                execution_metadata(
                    f"failure:charm:{application.charm}:status",
                    f"application:{application.status}:{normalize_message(application.message)}",
                )
            for unit in exc.wait_state.noncompliant_units.values():
                if unit is None:
                    continue
                execution_metadata(
                    f"failure:charm:{unit.charm}:status",
                    f"unit:{unit.status}:{normalize_message(unit.message)}",
                )
        elif isinstance(exc, CalledProcessError):
            cmd = " ".join(exc.cmd) if isinstance(exc.cmd, (list, tuple)) else exc.cmd
            execution_metadata("failure:cli:cmd", normalize_message(cmd))
            execution_metadata("failure:cli:return_code", str(exc.returncode))
            if exc.stdout:
                execution_metadata("failure:cli:stdout", normalize_message(exc.stdout))
            if exc.stderr:
                execution_metadata("failure:cli:stderr", normalize_message(exc.stderr))
