# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import subprocess
from pathlib import Path

import pytest

from .scheduler.states import State


@pytest.mark.state(requires=State.LOGS_COLLECTED)
def test_logs_privacy_check(
    logs_directory: Path,
    logger: logging.Logger,
) -> None:
    """Scan collected logs for secrets using TruffleHog.
    
    This test:
    1. Uses logs collected by the test_collect_logs bridge test
    2. Scans all logs with TruffleHog to detect secrets
    3. Fails if any secrets are found
    
    Requires: LOGS_COLLECTED state (provided by test_collect_logs)
    """
    logger.info(f"Scanning logs from {logs_directory} for secrets")
    
    # Check if docker is available
    try:
        subprocess.run(
            ["docker", "--version"],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        pytest.skip(f"Docker is not available: {e}")
    
    # Run TruffleHog in filesystem mode
    logger.info("Running TruffleHog secret scanner")
    
    # Run TruffleHog container
    docker_cmd = [
        "docker", "run", "--rm",
        "-v", f"{logs_directory}:/scan-logs",
        "ghcr.io/trufflesecurity/trufflehog@sha256:b356cc273ab8c786fe2a54f20d2bec1f67438df4ca070e5c7d5a1283e18917cb",
        "filesystem", "/scan-logs"
    ]
    
    result = subprocess.run(
        docker_cmd,
        capture_output=True,
        text=True,
        timeout=600,  # 10 minutes timeout for scanning
    )
    
    # Get TruffleHog output
    trufflehog_output = result.stdout + result.stderr
    
    # Log the scan results
    logger.info(f"TruffleHog exit code: {result.returncode}")
    if trufflehog_output:
        logger.info(f"TruffleHog output:\n{trufflehog_output}")
    
    # TruffleHog exits with non-zero code if secrets are found
    if result.returncode != 0:
        pytest.fail(
            f"Secrets detected in logs! TruffleHog found potential secrets.\n"
            f"Exit code: {result.returncode}\n"
            f"Scan output:\n{trufflehog_output}"
        )
    
    logger.info("No secrets detected in logs")
