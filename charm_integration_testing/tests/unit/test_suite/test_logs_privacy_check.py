# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_suite.test_logs_privacy_check import test_logs_privacy_check


@pytest.fixture
def log_dir() -> MagicMock:
    """Override log_dir fixture to return an empty directory mock."""
    mock_logs_dir = MagicMock(spec=Path)
    mock_logs_dir.iterdir.return_value = iter([])
    return mock_logs_dir


def test_logs_privacy_check_with_no_controllers(
    caplog: pytest.LogCaptureFixture,
    logger: logging.Logger,
    log_dir: MagicMock,
) -> None:
    """Test privacy check handles the case when logs directory is empty.

    This test verifies that the privacy check is skipped gracefully when
    the log directory is empty (simulating the case where no controllers
    collected logs).
    """
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        # Call the actual privacy check function
        test_logs_privacy_check(log_dir, logger)

    # Verify the test was skipped
    assert "log-dir is empty" in caplog.text


def test_logs_privacy_check_scans_archives_and_tolerates_bad_bytes(
    tmp_path: Path,
    logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TruffleHog is invoked against the whole log directory, with no exclusions, so
    archives (e.g. juju-crashdump tarballs) are still decoded and scanned for secrets.

    Also verifies the subprocess call tolerates non-UTF-8 bytes in TruffleHog's own
    stdout (e.g. leftover binary content it decoded from a scanned archive) instead of
    raising ``UnicodeDecodeError``, which is what issue #664 reported.
    """
    log_file = tmp_path / "juju-controller.log"
    log_file.write_text("nothing interesting here")

    version_check = MagicMock(returncode=0)
    scan_result = MagicMock(returncode=0, stdout="No secrets found.", stderr="")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        calls.append(cmd)
        if cmd[:2] == ["trufflehog", "--version"]:
            return version_check
        assert kwargs.get("errors") == "replace", "must tolerate non-UTF-8 bytes in TruffleHog output"
        return scan_result

    monkeypatch.setattr(subprocess, "run", fake_run)

    test_logs_privacy_check(tmp_path, logger)

    scan_cmd = calls[-1]
    assert scan_cmd == ["trufflehog", "filesystem", str(tmp_path), "--no-update", "--json", "--fail"]


def test_logs_privacy_check_redacts_secrets_in_logs_and_failure(
    tmp_path: Path,
    logger: logging.Logger,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When TruffleHog finds a secret, neither the logged output nor the failure message
    should contain the raw secret value; only TruffleHog's own pre-redacted summary should
    appear.
    """
    log_file = tmp_path / "unit-target-0.log"
    log_file.write_text("some log line")

    secret_value = "-----BEGIN PRIVATE KEY-----\nTOTALLY-SECRET-KEY-MATERIAL\n-----END PRIVATE KEY-----"
    finding = {
        "SourceMetadata": {"Data": {"Filesystem": {"file": str(log_file), "line": 1}}},
        "DetectorName": "PrivateKey",
        "Verified": False,
        "Raw": secret_value,
        "RawV2": "",
        "Redacted": "-----BEGIN PRIVATE KEY-----\nTOTALLY",
    }
    version_check = MagicMock(returncode=0)
    scan_result = MagicMock(returncode=183, stdout=json.dumps(finding) + "\n", stderr="")

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:2] == ["trufflehog", "--version"]:
            return version_check
        return scan_result

    monkeypatch.setattr(subprocess, "run", fake_run)

    caplog.clear()
    with caplog.at_level(logging.INFO):
        with pytest.raises(pytest.fail.Exception) as excinfo:
            test_logs_privacy_check(tmp_path, logger)

    assert secret_value not in caplog.text
    assert secret_value not in str(excinfo.value)
    assert "PrivateKey" in caplog.text
    assert "PrivateKey" in str(excinfo.value)
