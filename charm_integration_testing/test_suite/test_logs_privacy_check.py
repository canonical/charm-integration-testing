# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import subprocess  # nosec B404
from pathlib import Path

import pytest

from test_suite.scheduler.states import State

# TruffleHog exit codes
TRUFFLEHOG_NO_FINDINGS = 0
TRUFFLEHOG_FINDINGS_DETECTED = 1


def test_logs_privacy_check(
    debug_logs_directory: Path,
    logger: logging.Logger,
) -> None:
    """Scan collected logs for secrets using TruffleHog.

    This test scans collected logs with TruffleHog to detect secrets.

    Outcomes:
    - ERROR: Docker is unavailable or scan times out (test cannot run)
    - FAILED: Secrets are found in logs
    - PASSED: No secrets found
    """
    logger.info(f"Scanning logs from {debug_logs_directory} for secrets")

    # Check if docker is available
    try:
        subprocess.run(  # nosec B603, B607
            ["docker", "--version"],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise RuntimeError(f"Docker is not available (required for privacy check): {e}") from e

    # Run TruffleHog
    logger.info("Running TruffleHog secret scanner")

    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{debug_logs_directory}:/scan-logs:ro",
        "ghcr.io/trufflesecurity/trufflehog@sha256:b356cc273ab8c786fe2a54f20d2bec1f67438df4ca070e5c7d5a1283e18917cb",
        "filesystem",
        "/scan-logs",
    ]

    try:
        result = subprocess.run(  # nosec B603
            docker_cmd,
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
