# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.


import json
import logging
import os
import warnings
from datetime import timedelta
from pathlib import Path
from subprocess import CalledProcessError, run  # nosec
from typing import Any, Callable, Iterator

import pytest
from extensions import (
    ConfigureLivepatchServerExtension,
    PostgresqlDatabaseReplicationExtension,
    PostgresqlK8sDatabaseReplicationExtension,
    S3IntegratorMinIOBackendExtension,
    UnsealVaultJujuExtension,
    UnsealVaultK8sJujuExtension,
)
from juju import JujuBackend, JujuClient, JujuWaitTimeoutError
from juju_jubilant import JubilantBackend
from pytest import StashKey
from utils import normalize_string, normalize_string_multiline

KNOWN_FAILURE_EXCEPTIONS = (
    # JujuWaitTimeoutError,
    AssertionError,
    CalledProcessError,
)

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
def juju_client(
    juju_backend: JujuBackend, logger: logging.Logger, minio_client_file: Path | None, ubuntu_pro_token: str | None
) -> JujuClient:
    return JujuClient(
        juju_backend,
        logger,
        extensions=[
            ConfigureLivepatchServerExtension(juju_backend, logger, ubuntu_pro_token),
            PostgresqlDatabaseReplicationExtension(juju_backend, logger),
            PostgresqlK8sDatabaseReplicationExtension(juju_backend, logger),
            S3IntegratorMinIOBackendExtension(juju_backend, logger, minio_client_file),
            UnsealVaultJujuExtension(juju_backend, logger),
            UnsealVaultK8sJujuExtension(juju_backend, logger),
        ],
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--model", type=str, required=True, help="Juju model to test in")


@pytest.fixture
def model(request: pytest.FixtureRequest) -> str:
    option = request.config.getoption("--model")
    assert isinstance(option, str)
    return option


@pytest.fixture
def minio_client_file() -> Path | None:
    file_path = os.environ.get("MINIO_CLIENT_FILE")
    if file_path:
        file_path = file_path.strip()
    return Path(file_path) if file_path else None


@pytest.fixture
def ubuntu_pro_token() -> str | None:
    token = os.environ.get("UBUNTU_PRO_TOKEN")
    if token:
        token = token.strip()
    return token if token else None


failure_message = StashKey[str]()
error_message = StashKey[str]()
skipped_message = StashKey[str]()
failure_exception = StashKey[BaseException]()

# Get failure message for logging
@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Iterator[None]:
    result = yield
    assert result is not None
    report = result.get_result()

    unexpected_error = False

    if call.excinfo is not None:
        exception_type = call.excinfo.type
        print("-=-=-=-=-=-=-=-=-=-=-=-=-=-=-")
        print(exception_type)
        # Don't interfere with pytest's built-in exceptions (skip, xfail, etc.)
        if exception_type.__name__ in ('Skipped', 'XFailed', 'Exit'):
            pass
        elif exception_type in KNOWN_FAILURE_EXCEPTIONS:
            # Known failures: ensure they're marked as "failed" not "error"
            if report.outcome == "error":
                report.outcome = "failed"
        else:
            # Unexpected errors: keep failed=True, but force JUnit to emit <error>
            unexpected_error = True
            if report.outcome != "failed":
                report.outcome = "failed"
            if report.when == "call":
                report.when = "setup"

    # Save failure message
    if report.failed:
        # Adapted from https://docs.pytest.org/en/stable/_modules/_pytest/junitxml.html
        reprcrash = getattr(report.longrepr, "reprcrash", None)
        if reprcrash is not None:
            item.stash[failure_message] = reprcrash.message
        else:
            item.stash[failure_message] = str(report.longrepr)
        if unexpected_error:
            item.stash[error_message] = item.stash[failure_message]

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
) -> Iterator[None]:
    # Enforce fixture execution order
    _ = record_execution_metadata

    # Print starting state
    juju_client.print_status(model=model)

    # Log starting
    logger.info(f"Starting {request.node.name}")

    yield

    # Log error
    if error_message in request.node.stash:
        logger.error(f"Error in {request.node.name}: {request.node.stash[error_message]}")
    elif failure_message in request.node.stash:
        logger.error(f"Failure in {request.node.name}: {request.node.stash[failure_message]}")
    elif skipped_message in request.node.stash:
        logger.info(f"Skipped {request.node.name}: {request.node.stash[skipped_message]}")
    else:
        logger.info(f"Successfully ran {request.node.name}")

    # Log ending state
    juju_client.print_status(model=model)


@pytest.fixture(autouse=True)
def assert_idle(juju_client: JujuClient, model: str, print_setup_and_teardown_info: None) -> None:
    # Enforce fixture execution order
    _ = print_setup_and_teardown_info

    try:
        juju_client.idle_for_period(model=model, timeout=timedelta(seconds=30), count=5)
    except JujuWaitTimeoutError as e:
        pytest.skip(str(e))


@pytest.fixture
def execution_metadata(record_property: Callable[[str, object], None]) -> Iterator[Callable[[str, str], None]]:
    # Create a function for adding and deduplicating metadata
    metadata: dict[str, set[str]] = {}

    def add(category: str, value: str) -> None:
        if not category.strip() or not value.strip():
            return
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
        record_property(category, json.dumps([value for value in sorted(values)]))


@pytest.fixture(autouse=True)
def record_execution_metadata(
    record_warning_execution_metadata: None,
    record_failure_execution_metadata: None,
    record_juju_execution_metadata: None,
    record_charms_and_revisions_execution_metadata: None,
    record_pipeline_version_execution_metadata: None,
) -> None:
    # Save various execution metadata
    _ = record_warning_execution_metadata
    _ = record_failure_execution_metadata
    _ = record_juju_execution_metadata
    _ = record_charms_and_revisions_execution_metadata
    _ = record_pipeline_version_execution_metadata


@pytest.fixture
def record_warning_execution_metadata(execution_metadata: Callable[[str, str | int], None]) -> Iterator[None]:
    # Capture all warnings
    # Pytest normally captures warnings, but does not expose them until after the test report is made
    captured_warnings = []
    with warnings.catch_warnings(record=True) as warnings_list:
        # Let the test run
        yield

        # Save all warnings
        for warning in warnings_list:
            execution_metadata("warning:message", normalize_string(f"{warning.category.__name__}: {warning.message}"))
            captured_warnings.append(warning)

    # Re-emit all warnings so they show up in the test summary
    for warning in captured_warnings:
        warnings.warn_explicit(
            message=warning.message,
            category=warning.category,
            filename=warning.filename,
            lineno=warning.lineno,
            source=warning.source,
        )


def record_charms_and_revisions_execution_metadata_instantaneous(
    juju_client: JujuClient, model: str, execution_metadata: Callable[[str, str | int], None]
) -> None:
    # Get all charm revisions
    for charm, revision in juju_client.get_charm_revisions(model=model):
        # Save the charm
        execution_metadata("charm", charm)
        # Save the revision
        execution_metadata(f"charm:{charm}:revision", str(revision))


@pytest.fixture
def record_charms_and_revisions_execution_metadata(
    juju_client: JujuClient, model: str, execution_metadata: Callable[[str, str | int], None]
) -> Iterator[None]:
    # Save all charms and revisions at start of test
    record_charms_and_revisions_execution_metadata_instantaneous(juju_client, model, execution_metadata)

    # Let the test run
    yield

    # Save all charms and revisions at end of test
    record_charms_and_revisions_execution_metadata_instantaneous(juju_client, model, execution_metadata)


@pytest.fixture
def record_failure_execution_metadata(
    request: pytest.FixtureRequest, execution_metadata: Callable[[str, str | int], None]
) -> Iterator[None]:
    # Let the test run
    yield

    # Save the failure message
    if failure_message in request.node.stash:
        if error_message in request.node.stash:
            execution_metadata("error:message", normalize_string(request.node.stash[error_message]))
        else:
            execution_metadata("failure:message", normalize_string(request.node.stash[failure_message]))

    # Save the skip message
    if skipped_message in request.node.stash:
        execution_metadata("skipped:message", normalize_string(request.node.stash[skipped_message]))

    # Save extra metadata from exception
    if failure_exception in request.node.stash:
        exc = request.node.stash[failure_exception]

        # Save state from wait timeout
        is_error = error_message in request.node.stash

        if isinstance(exc, JujuWaitTimeoutError):
            for application in exc.wait_state.noncompliant_applications.values():
                if application is None:
                    continue
                execution_metadata(
                    f"failure:charm:{application.charm}:status",
                    f"application:{application.status}:{normalize_string(application.message)}",
                )
            for unit in exc.wait_state.noncompliant_units.values():
                if unit is None:
                    continue
                execution_metadata(
                    f"failure:charm:{unit.charm}:status",
                    f"unit:{unit.status}:{normalize_string(unit.message)}",
                )
            for unit_agent in exc.wait_state.noncompliant_unit_agents.values():
                if unit_agent is None:
                    continue
                execution_metadata(
                    f"failure:charm:{unit_agent.charm}:status",
                    f"unit_agent:{unit_agent.status}:{normalize_string(unit_agent.message)}",
                )
        elif isinstance(exc, CalledProcessError):
            cmd = " ".join(exc.cmd) if isinstance(exc.cmd, (list, tuple)) else exc.cmd
            execution_metadata("failure:cli:cmd", normalize_string(cmd))
            execution_metadata("failure:cli:return_code", str(exc.returncode))
            if exc.stdout:
                for line in normalize_string_multiline(exc.stdout):
                    execution_metadata("failure:cli:stdout", line)
            if exc.stderr:
                for line in normalize_string_multiline(exc.stderr):
                    execution_metadata("failure:cli:stderr", line)
        
        elif is_error:
            # For other unexpected errors, log the error line by line
            for line in normalize_string_multiline(str(exc)):
                execution_metadata("error:exception:", line)
        


@pytest.fixture
def record_juju_execution_metadata(
    juju_client: JujuClient, model: str, execution_metadata: Callable[[str, str | int], None]
) -> Iterator[None]:
    # Let the test run
    yield

    # Save Juju version
    juju_version = juju_client.version(model)
    execution_metadata("juju:version", juju_version)


@pytest.fixture
def record_pipeline_version_execution_metadata(
    execution_metadata: Callable[[str, str | int], None],
    request: pytest.FixtureRequest,
) -> None:
    pipeline_path: Path = Path(request.config.rootpath) / ".github" / "workflows" / "charm-testing.yaml"

    # Get repository commit hash
    repository_version_command = ["git", "--no-pager", "log", "-n", "1", "--pretty=format:%h"]
    repository_result = run(repository_version_command, capture_output=True, text=True)  # nosec B603
    if repository_result.returncode == 0:
        execution_metadata("pipeline:ref", repository_result.stdout.strip())
    else:
        warnings.warn(f"Failed to get git commit hash: {repository_result.stderr.strip()}")

    # Get repository tag if it exists
    repository_tag_command = ["git", "describe", "--tags", "--exact-match", repository_result.stdout.strip()]
    repository_tag_result = run(repository_tag_command, capture_output=True, text=True)  # nosec B603
    if repository_tag_result.returncode == 0:
        execution_metadata("pipeline:tag", repository_tag_result.stdout.strip())
    elif "no tag exactly matches" in repository_tag_result.stderr.lower():
        warnings.warn("No tag exists in git repo pointing to this commit.")
    else:
        warnings.warn(f"Failed to get git tag: {repository_tag_result.stderr.strip()}")

    # Get pipeline workflow hash if file exists
    if pipeline_path.exists():
        pipeline_version_command = [
            "git",
            "hash-object",
            "--",
            str(pipeline_path.resolve()),
        ]
        pipeline_result = run(pipeline_version_command, capture_output=True, text=True)  # nosec B603
        if pipeline_result.returncode == 0:
            execution_metadata("pipeline:workflow_hash", pipeline_result.stdout.strip())
        else:
            warnings.warn(f"Failed to get pipeline workflow hash: {pipeline_result.stderr.strip()}")
    else:
        warnings.warn(f"Pipeline file not found: {pipeline_path}")
