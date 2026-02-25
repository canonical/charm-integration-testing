# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Core Juju fixtures: client, model, and supporting options.

All fixtures in this module are available to every test in the suite.
They are loaded via ``pytest_plugins`` in the top-level conftest.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import cast

import pytest
from extensions import (
    ConfigureLivepatchServerExtension,
    PostgresqlDatabaseReplicationExtension,
    PostgresqlK8sDatabaseReplicationExtension,
    S3IntegratorMinIOBackendExtension,
    TemporalExtension,
    UnsealVaultJujuExtension,
    UnsealVaultK8sJujuExtension,
)
from juju import JujuBackend, JujuClient
from juju_jubilant import JubilantBackend


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register Juju-related CLI options shared across all test phases."""
    parser.addoption("--model", type=str, required=True, help="Juju model to test in.")
    parser.addoption(
        "--bundles",
        nargs="*",
        type=str,
        default=[],
        help="Bundle file paths to deploy (used by deploy and idempotent-redeploy phases).",
    )
    parser.addoption(
        "--target-application",
        type=str,
        default=None,
        help="Application under test (used by integration tests).",
    )
    parser.addoption(
        "--target-endpoint",
        type=str,
        default=None,
        help="Endpoint on the target application used for the integration under test.",
    )
    parser.addoption(
        "--neighbor-application",
        type=str,
        default=None,
        help="Neighbor application that integrates with the target (used by integration tests).",
    )
    parser.addoption(
        "--neighbor-endpoint",
        type=str,
        default=None,
        help="Endpoint on the neighbor application used for the integration under test.",
    )


@pytest.fixture
def model(request: pytest.FixtureRequest) -> str:
    """The Juju model name passed via ``--model``."""
    option = request.config.getoption("--model")
    assert isinstance(option, str)
    return option


@pytest.fixture
def bundles(request: pytest.FixtureRequest) -> list[str]:
    """Bundle file paths passed via ``--bundles``."""
    option = request.config.getoption("--bundles")
    assert isinstance(option, list)
    return option


@pytest.fixture
def target_application(request: pytest.FixtureRequest) -> str:
    """Name of the charm application under test, passed via ``--target-application``."""
    value = request.config.getoption("--target-application")
    if not value:
        pytest.fail("--target-application is required by this test but was not provided.")
    return cast(str, value)


@pytest.fixture
def target_endpoint(request: pytest.FixtureRequest) -> str:
    """Juju endpoint on the target application, passed via ``--target-endpoint``."""
    value = request.config.getoption("--target-endpoint")
    if not value:
        pytest.fail("--target-endpoint is required by this test but was not provided.")
    return cast(str, value)


@pytest.fixture
def neighbor_application(request: pytest.FixtureRequest) -> str:
    """Name of the neighbor application, passed via ``--neighbor-application``."""
    value = request.config.getoption("--neighbor-application")
    if not value:
        pytest.fail("--neighbor-application is required by this test but was not provided.")
    return cast(str, value)


@pytest.fixture
def neighbor_endpoint(request: pytest.FixtureRequest) -> str:
    """Juju endpoint on the neighbor application, passed via ``--neighbor-endpoint``."""
    value = request.config.getoption("--neighbor-endpoint")
    if not value:
        pytest.fail("--neighbor-endpoint is required by this test but was not provided.")
    return cast(str, value)


@pytest.fixture
def minio_client_file() -> Path | None:
    """Path to the MinIO client binary, read from ``MINIO_CLIENT_FILE`` env var."""
    file_path = os.environ.get("MINIO_CLIENT_FILE")
    if file_path:
        file_path = file_path.strip()
    return Path(file_path) if file_path else None


@pytest.fixture
def ubuntu_pro_token() -> str | None:
    """Ubuntu Pro token, read from ``UBUNTU_PRO_TOKEN`` env var."""
    token = os.environ.get("UBUNTU_PRO_TOKEN")
    if token:
        token = token.strip()
    return token if token else None


@pytest.fixture
def logger() -> logging.Logger:
    """Application logger with jubilant sub-loggers quieted to WARNING."""
    logging.getLogger("jubilant").setLevel(logging.WARNING)
    logging.getLogger("jubilant.wait").setLevel(logging.WARNING)
    return logging.getLogger()


@pytest.fixture
def juju_backend() -> JujuBackend:
    """The low-level Juju backend (jubilant implementation)."""
    return JubilantBackend()


@pytest.fixture
def juju_client(
    juju_backend: JujuBackend,
    logger: logging.Logger,
    minio_client_file: Path | None,
    ubuntu_pro_token: str | None,
) -> JujuClient:
    """A fully-configured :class:`~juju.JujuClient` with all known extensions."""
    return JujuClient(
        juju_backend,
        logger,
        extensions=[
            ConfigureLivepatchServerExtension(juju_backend, logger, ubuntu_pro_token),
            PostgresqlDatabaseReplicationExtension(juju_backend, logger),
            PostgresqlK8sDatabaseReplicationExtension(juju_backend, logger),
            S3IntegratorMinIOBackendExtension(juju_backend, logger, minio_client_file),
            TemporalExtension(juju_backend, logger),
            UnsealVaultJujuExtension(juju_backend, logger),
            UnsealVaultK8sJujuExtension(juju_backend, logger),
        ],
    )
