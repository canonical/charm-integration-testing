# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Execution-metadata fixtures: record structured data into JUnit XML properties.

All fixtures here are loaded via ``pytest_plugins`` in the top-level conftest.
They attach structured metadata to each test report so that downstream tooling
(dashboards, flakiness trackers, etc.) can filter and aggregate results.

Collected metadata categories include:
- ``charm`` / ``charm:<name>:revision`` — charms deployed during the test.
- ``integration`` — active Juju integrations at start/end.
- ``juju:version`` — Juju version in the tested model.
- ``pipeline:ref`` / ``pipeline:tag`` / ``pipeline:workflow_hash`` — CI provenance.
- ``failure:message`` / ``skipped:message`` — human-readable outcome strings.
- ``warning:message`` — any Python warnings raised during the test.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from subprocess import CalledProcessError, run  # nosec B404 B603
from typing import Callable, Iterator

import pytest
from juju import JujuClient, JujuWaitTimeoutError
from pytest import StashKey
from utils import normalize_string, normalize_string_multiline

# ---------------------------------------------------------------------------
# Stash keys — used to pass data from the runtest hook to fixtures.
# ---------------------------------------------------------------------------

#: Human-readable failure message (present when `report.failed`).
failure_message: StashKey[str] = StashKey()

#: Set when the failure is due to an *unexpected* exception type.
error_message: StashKey[str] = StashKey()

#: Human-readable skip/xfail reason (present when `report.skipped`).
skipped_message: StashKey[str] = StashKey()

#: The actual exception that caused a failure, for structured introspection.
failure_exception: StashKey[BaseException] = StashKey()


# ---------------------------------------------------------------------------
# Core metadata accumulator
# ---------------------------------------------------------------------------


@pytest.fixture
def execution_metadata(
    record_property: Callable[[str, object], None],
) -> Iterator[Callable[[str, str], None]]:
    """Yield a callable that accumulates ``(category, value)`` metadata pairs.

    Multiple values per category are collected into a sorted set and written
    to the JUnit XML as a single JSON-encoded list under the category key.
    This sidesteps the JUnit limitation of one value per property key.

    Usage::

        def test_something(execution_metadata):
            execution_metadata("charm", "postgresql")
            execution_metadata("charm:postgresql:revision", "42")
    """
    metadata: dict[str, set[str]] = {}

    def add(category: str, value: str) -> None:
        if not category.strip() or not value.strip():
            return
        metadata.setdefault(category, set()).add(value)

    yield add

    for category, values in metadata.items():
        record_property(category, json.dumps(sorted(values)))


# ---------------------------------------------------------------------------
# Composite record_execution_metadata fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def record_execution_metadata(
    record_warning_execution_metadata: None,
    record_failure_execution_metadata: None,
    record_juju_execution_metadata: None,
    record_charms_and_revisions_execution_metadata: None,
    record_pipeline_version_execution_metadata: None,
) -> None:
    """Autouse fixture that activates all metadata-recording sub-fixtures.

    The individual sub-fixtures are declared as dependencies so that pytest
    sets them up and tears them down in the correct order.
    """
    # Dependencies are declared above purely to enforce ordering; nothing
    # additional needs to happen here.
    _ = record_warning_execution_metadata
    _ = record_failure_execution_metadata
    _ = record_juju_execution_metadata
    _ = record_charms_and_revisions_execution_metadata
    _ = record_pipeline_version_execution_metadata


# ---------------------------------------------------------------------------
# Individual metadata sub-fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def record_warning_execution_metadata(
    execution_metadata: Callable[[str, str], None],
) -> Iterator[None]:
    """Capture Python warnings raised during the test and record them as metadata."""
    captured: list[warnings.WarningMessage] = []

    with warnings.catch_warnings(record=True) as warnings_list:
        yield

        for w in warnings_list:
            execution_metadata(
                "warning:message",
                normalize_string(f"{w.category.__name__}: {w.message}"),
            )
            captured.append(w)

    # Re-emit so that they still appear in pytest's warning summary.
    for w in captured:
        warnings.warn_explicit(
            message=w.message,
            category=w.category,
            filename=w.filename,
            lineno=w.lineno,
            source=w.source,
        )


@pytest.fixture
def record_charms_and_revisions_execution_metadata(
    juju_client: JujuClient,
    model: str,
    execution_metadata: Callable[[str, str], None],
) -> Iterator[None]:
    """Record deployed charm names, revisions, and active integrations.

    Metadata is captured at both the start and end of the test so that any
    changes (e.g. charm upgrades performed by the test itself) are reflected.
    """
    _snapshot_charms_and_integrations(juju_client, model, execution_metadata)
    yield
    _snapshot_charms_and_integrations(juju_client, model, execution_metadata)


def _snapshot_charms_and_integrations(
    juju_client: JujuClient,
    model: str,
    execution_metadata: Callable[[str, str], None],
) -> None:
    """Take an instantaneous snapshot of charms, revisions, and integrations."""
    applications = juju_client.list_applications(model=model)

    for app_info in applications.values():
        execution_metadata("charm", app_info.charm)
        execution_metadata(f"charm:{app_info.charm}:revision", str(app_info.revision))

    for integration in juju_client.list_integrations(model=model):
        provider_charm = applications[integration.provider.application].charm
        requirer_charm = applications[integration.requirer.application].charm
        integration_str = (
            f"{provider_charm}:{integration.provider.endpoint}"
            f"/{integration.interface}/"
            f"{requirer_charm}:{integration.requirer.endpoint}"
        )
        execution_metadata("integration", integration_str)


@pytest.fixture
def record_failure_execution_metadata(
    request: pytest.FixtureRequest,
    execution_metadata: Callable[[str, str], None],
) -> Iterator[None]:
    """After the test, record failure/skip messages and structured exception info."""
    yield

    if failure_message in request.node.stash:
        execution_metadata(
            "failure:message",
            normalize_string(request.node.stash[failure_message]),
        )

    if skipped_message in request.node.stash:
        execution_metadata(
            "skipped:message",
            normalize_string(request.node.stash[skipped_message]),
        )

    if failure_exception in request.node.stash:
        exc = request.node.stash[failure_exception]
        _record_exception_metadata(exc, execution_metadata)

    # Flag whether this was a known/expected failure type.
    if error_message in request.node.stash:
        execution_metadata("failure:expected", "false")
    elif failure_message in request.node.stash:
        execution_metadata("failure:expected", "true")


def _record_exception_metadata(
    exc: BaseException,
    execution_metadata: Callable[[str, str], None],
) -> None:
    """Extract structured metadata from recognised exception types."""
    if isinstance(exc, JujuWaitTimeoutError):
        for app in exc.wait_state.noncompliant_applications.values():
            if app is None:
                continue
            execution_metadata(
                f"failure:charm:{app.charm}:status",
                f"application:{app.status}:{normalize_string(app.message)}",
            )
        for unit in exc.wait_state.noncompliant_units.values():
            if unit is None:
                continue
            execution_metadata(
                f"failure:charm:{unit.charm}:status",
                f"unit:{unit.status}:{normalize_string(unit.message)}",
            )
        for agent in exc.wait_state.noncompliant_unit_agents.values():
            if agent is None:
                continue
            execution_metadata(
                f"failure:charm:{agent.charm}:status",
                f"unit_agent:{agent.status}:{normalize_string(agent.message)}",
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


@pytest.fixture
def record_juju_execution_metadata(
    juju_client: JujuClient,
    model: str,
    execution_metadata: Callable[[str, str], None],
) -> Iterator[None]:
    """Record the Juju version used in the tested model."""
    yield
    execution_metadata("juju:version", juju_client.version(model))


@pytest.fixture
def record_pipeline_version_execution_metadata(
    execution_metadata: Callable[[str, str], None],
    request: pytest.FixtureRequest,
) -> None:
    """Record CI provenance: git commit, tag, and workflow file hash."""
    pipeline_path: Path = Path(request.config.rootpath) / ".github" / "workflows" / "charm-testing.yaml"

    # Git commit hash
    result = run(  # nosec B603 B607
        ["git", "--no-pager", "log", "-n", "1", "--pretty=format:%h"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        ref = result.stdout.strip()
        execution_metadata("pipeline:ref", ref)

        # Git tag (only if one points exactly to this commit)
        tag_result = run(  # nosec B603 B607
            ["git", "describe", "--tags", "--exact-match", ref],
            capture_output=True,
            text=True,
        )
        if tag_result.returncode == 0:
            execution_metadata("pipeline:tag", tag_result.stdout.strip())
        elif "no tag exactly matches" not in tag_result.stderr.lower():
            warnings.warn(f"Failed to get git tag: {tag_result.stderr.strip()}")
    else:
        warnings.warn(f"Failed to get git commit hash: {result.stderr.strip()}")

    # Pipeline workflow file hash
    if pipeline_path.exists():
        hash_result = run(  # nosec B603 B607
            ["git", "hash-object", "--", str(pipeline_path.resolve())],
            capture_output=True,
            text=True,
        )
        if hash_result.returncode == 0:
            execution_metadata("pipeline:workflow_hash", hash_result.stdout.strip())
        else:
            warnings.warn(f"Failed to get pipeline workflow hash: {hash_result.stderr.strip()}")
    else:
        warnings.warn(f"Pipeline workflow file not found: {pipeline_path}")
