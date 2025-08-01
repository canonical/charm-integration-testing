# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Callable

import pytest
from extensions import S3IntegratorMinIOBackendExtension, UnsealVaultJujuExtension, UnsealVaultK8sJujuExtension
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
    else:
        logger.info(f"Successfully ran {request.node.name}")

    # Log ending state
    juju_client.print_status(model=model)


@pytest.fixture(autouse=True)
def assert_idle(juju_client: JujuClient, model: str, print_setup_and_teardown_info: None):
    # Enforce fixture execution order
    _ = print_setup_and_teardown_info

    try:
        juju_client.idle_for_period(model=model, timeout=timedelta(seconds=15), idle_period=timedelta(seconds=5))
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
        record_property(category, json.dumps([str(value) for value in sorted(values)]))


@pytest.fixture(autouse=True)
def record_execution_metadata(
    record_charms_and_revisions_execution_metadata: None,
):
    # Save various execution metadata
    _ = record_charms_and_revisions_execution_metadata


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
