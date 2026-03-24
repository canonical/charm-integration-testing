# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
import shutil
import subprocess  # nosec B404
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
from juju import JujuClient

from .scheduler.states import State


@pytest.fixture(scope="session")
def logs_directory() -> Generator[Path, None, None]:
    """Provide a session-scoped directory for storing collected logs."""
    logs_dir = Path(tempfile.mkdtemp(prefix="juju-logs-"))
    yield logs_dir
    # Cleanup after all tests complete
    shutil.rmtree(logs_dir, ignore_errors=True)


@pytest.mark.state(requires=State.DEPLOYED, provides=State.LOGS_COLLECTED, bridge=True)
def test_collect_logs(
    juju_client: JujuClient,
    model: str,
    target_application: str,
    bundle: Path,
    logs_directory: Path,
    logger: logging.Logger,
) -> None:
    """Collect Juju debug logs from the model.

    This is a bridge test that collects logs for potential use by downstream tests.
    """
    logger.info(f"Collecting debug logs from model {model} to {logs_directory}")

    result = subprocess.run(  # nosec B603, B607
        ["juju", "debug-log", "--model", model, "--replay", "--no-tail"],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )

    debug_log_file = logs_directory / "debug.log"
    debug_log_file.write_text(result.stdout)

    logger.info(f"Collected {len(result.stdout)} bytes of debug logs to {debug_log_file}")
