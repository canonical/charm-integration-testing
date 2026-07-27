# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import subprocess  # nosec B404
from pathlib import Path

import pytest

# TruffleHog exit codes
TRUFFLEHOG_NO_FINDINGS = 0
TRUFFLEHOG_FINDINGS_DETECTED = 1


# no state marker so it runs last
def test_logs_privacy_check(
    log_dir: Path | None,
    logger: logging.Logger,
) -> None:
    """Scan collected logs for secrets using TruffleHog.

    This test scans logs from the log directory (passed via --log-dir) with
    TruffleHog to detect secrets.

    Outcomes:
    - SKIPPED: No logs provided
    - ERROR: TruffleHog is unavailable or scan times out (test cannot run)
    - FAILED: Secrets are found in logs
    - PASSED: No secrets found
    """
    if log_dir is None:
        pytest.skip("log-dir parameter not provided (--log-dir)")
    if not any(log_dir.iterdir()):
        pytest.skip("log-dir is empty, no logs to scan")

    logger.info(f"Scanning logs from {log_dir} for secrets")

    # Check if trufflehog is available
    try:
        subprocess.run(  # nosec B603, B607
            ["trufflehog", "--version"],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise RuntimeError(f"TruffleHog CLI is not available (required for privacy check): {e}") from e

    # Run TruffleHog
    logger.info("Running TruffleHog secret scanner")

    trufflehog_cmd = [
        "trufflehog",
        "filesystem",
        str(log_dir),
    ]

    try:
        # TruffleHog natively decompresses and scans archives (e.g. juju-crashdump
        # tarballs) alongside plaintext logs. That can surface non-UTF-8 bytes from
        # binary payloads in its own stdout; `errors="replace"` tolerates those bytes
        # instead of crashing with a UnicodeDecodeError, without skipping any content.
        result = subprocess.run(  # nosec B603
            trufflehog_cmd,
            capture_output=True,
            text=True,
            errors="replace",
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
