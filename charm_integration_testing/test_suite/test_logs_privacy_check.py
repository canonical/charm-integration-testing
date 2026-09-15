# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
import subprocess  # nosec B404
from pathlib import Path
from typing import Any

import pytest

# TruffleHog exit codes. Without --fail, TruffleHog only returns a non-zero
# code for *verified* secrets, silently exiting 0 for unverified findings
# (e.g. private keys, which can't be verified against a live endpoint).
# --fail makes it exit 183 for any finding, verified or not.
TRUFFLEHOG_NO_FINDINGS = 0
TRUFFLEHOG_FINDINGS_DETECTED = 183

UNRECOGNIZED_OUTPUT_MARKER = "[unrecognized TruffleHog output line - redacted for safety]"


def _redact_finding_line(line: str) -> str:
    """Summarize a single line of TruffleHog's ``--json`` output without its secret value.

    TruffleHog emits one JSON object per line: either a finding (with a ``Raw``/``RawV2``
    field holding the actual matched secret) or a diagnostic/progress log line.

    Finding objects (valid JSON with a ``DetectorName`` field) are summarized to include only the
    name (i.e. not reveal the secret itself in the TruffleHog report)

    Lines that can't be properly parsed are replaced with a constant marker to avoid accidentally
    leaking secrets.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return UNRECOGNIZED_OUTPUT_MARKER

    if not isinstance(data, dict) or "DetectorName" not in data:
        return UNRECOGNIZED_OUTPUT_MARKER

    source_metadata = data.get("SourceMetadata")
    metadata_data = source_metadata.get("Data") if isinstance(source_metadata, dict) else None
    first_source = next(iter(metadata_data.values()), None) if isinstance(metadata_data, dict) else None
    source: dict[str, Any] = first_source if isinstance(first_source, dict) else {}
    location = f"{source.get('file', 'unknown file')}:{source.get('line', '?')}"
    return (
        f"Detector={data.get('DetectorName', 'unknown')} "
        f"Verified={data.get('Verified', False)} "
        f"Location={location} "
        f"Secret(redacted)={data.get('Redacted', '<redacted>')}"
    )


def _redact_trufflehog_output(output: str) -> str:
    """Redact secret values out of TruffleHog's raw output before it gets logged."""
    return "\n".join(_redact_finding_line(line) for line in output.splitlines() if line.strip())


# no state marker so it runs last
def test_logs_privacy_check(
    log_dir: Path | None,
    logger: logging.Logger,
) -> None:
    """Scan collected logs for secrets using TruffleHog.

    This test scans logs from the log directory (passed via --log-dir) with
    TruffleHog to detect secrets. Any secret values found are redacted before
    being logged or included in the failure message.

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
        "--no-update",  # avoid a spurious failure if the binary's install dir isn't writable
        "--json",  # structured output so findings can be redacted before logging
        "--fail",  # exit non-zero for any finding, not just verified ones
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

    # Redact secret values before they ever reach a log line or failure message.
    trufflehog_output = _redact_trufflehog_output(result.stdout + result.stderr)

    logger.info(f"TruffleHog exit code: {result.returncode}")
    if trufflehog_output:
        logger.info(f"TruffleHog output (secrets redacted):\n{trufflehog_output}")

    # TruffleHog exit codes:
    if result.returncode == TRUFFLEHOG_FINDINGS_DETECTED:
        pytest.fail(f"TruffleHog found potential secrets.\n" f"Scan output (secrets redacted):\n{trufflehog_output}")
    elif result.returncode == TRUFFLEHOG_NO_FINDINGS:
        logger.info("No secrets found in logs.")
    else:
        output_str = (
            f"Scan output (secrets redacted):\n{trufflehog_output}"
            if trufflehog_output
            else "No output from TruffleHog."
        )
        pytest.fail(f"TruffleHog scan failed with unexpected exit code {result.returncode}.\n" f"{output_str}")
