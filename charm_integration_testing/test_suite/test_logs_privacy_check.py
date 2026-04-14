# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import subprocess  # nosec B404
import tempfile
from pathlib import Path
from typing import Iterator

import pytest
from juju import JujuBackend

# TruffleHog exit codes
TRUFFLEHOG_NO_FINDINGS = 0
TRUFFLEHOG_FINDINGS_DETECTED = 1


@pytest.fixture
def debug_logs_directory(juju_backend: JujuBackend, model: str, logger: logging.Logger) -> Iterator[Path]:
    """Provide a directory with collected debug logs from Juju.

    Logs are collected during test setup (not teardown), ensuring they're available
    immediately for the test to use. This fixture is self-contained and doesn't
    depend on other fixtures running first.

    The temporary directory is automatically cleaned up after the test.
    """
    logger.info("Collecting debug logs...")

    with tempfile.TemporaryDirectory(prefix="juju-logs-") as temp_dir:
        logs_dir = Path(temp_dir)
        logger.info(f"Collecting debug logs from model {model} to {logs_dir}")

        debug_log_file = logs_dir / "debug.log"

        try:
            debug_log = juju_backend.client.model(model).debug_log()  # type: ignore[attr-defined]
            debug_log_file.write_text(debug_log)

            log_size = debug_log_file.stat().st_size
            logger.info(f"Collected {log_size} bytes of debug logs to {debug_log_file}")

            yield logs_dir

        except Exception:
            logger.exception("Failed to collect debug logs")


# no state marker so it runs last
def test_logs_privacy_check(
    debug_logs_directory: Path,
    logger: logging.Logger,
) -> None:
    """Scan collected logs for secrets using TruffleHog.

    This test scans collected logs with TruffleHog to detect secrets.

    Outcomes:
    - ERROR: TruffleHog is unavailable or scan times out (test cannot run)
    - FAILED: Secrets are found in logs
    - PASSED: No secrets found
    """
    logger.info(f"Scanning logs from {debug_logs_directory} for secrets")

    # Check if trufflehog is available
    try:
        subprocess.run(  # nosec B603, B607
            ["trufflehog", "--version"],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise RuntimeError(f"Error checking for TruffleHog CLI: {e}\n") from e

    # Run TruffleHog
    logger.info("Running TruffleHog secret scanner")

    trufflehog_cmd = [
        "trufflehog",
        "filesystem",
        str(debug_logs_directory),
    ]

    try:
        result = subprocess.run(  # nosec B603
            trufflehog_cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minutes timeout for scanning
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"TruffleHog scan timed out after {e.timeout}s (required for privacy check)") from e

    # Get TruffleHog output
    trufflehog_output = result.stdout + result.stderr

    logger.info(f"TruffleHog exit code: {result.returncode}")
    if trufflehog_output:
        logger.info(f"TruffleHog output:\n{trufflehog_output}")

    # TruffleHog exit codes:
    if result.returncode == TRUFFLEHOG_FINDINGS_DETECTED:
        pytest.fail(f"TruffleHog found potential secrets.\n" f"Scan output:\n{trufflehog_output}")
    elif result.returncode == TRUFFLEHOG_NO_FINDINGS:
        logger.info("No secrets found in logs.")
    else:
        output_str = f"Scan output:\n{trufflehog_output}" if trufflehog_output else "No output from TruffleHog."
        pytest.fail(f"TruffleHog scan failed with unexpected exit code {result.returncode}.\n" f"{output_str}")
