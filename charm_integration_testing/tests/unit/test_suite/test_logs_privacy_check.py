# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_suite.test_logs_privacy_check import UNRECOGNIZED_OUTPUT_MARKER, test_logs_privacy_check


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
    scan_result = MagicMock(returncode=0, stdout=json.dumps({"level": "info-0", "msg": "no secrets found"}), stderr="")
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


def test_logs_privacy_check_redacts_unrecognized_output_lines(
    tmp_path: Path,
    logger: logging.Logger,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any TruffleHog output line that isn't a recognized finding object (valid JSON with
    a ``DetectorName`` field) must be replaced with a constant marker, never passed through
    verbatim. This covers both invalid JSON and JSON that lacks ``DetectorName`` -- we
    can't assume either is free of secret material (e.g. a future TruffleHog schema change,
    or an error line that unexpectedly echoes scanned content).
    """
    log_file = tmp_path / "unit-target-0.log"
    log_file.write_text("some log line")

    not_json_line = "some plain-text diagnostic line that happens to include token=abc123"
    json_without_detector = json.dumps({"level": "error", "msg": "could not read chunk", "secret_looking": "abc123"})
    stdout = f"{not_json_line}\n{json_without_detector}\n"

    version_check = MagicMock(returncode=0)
    scan_result = MagicMock(returncode=0, stdout=stdout, stderr="")

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:2] == ["trufflehog", "--version"]:
            return version_check
        return scan_result

    monkeypatch.setattr(subprocess, "run", fake_run)

    caplog.clear()
    with caplog.at_level(logging.INFO):
        test_logs_privacy_check(tmp_path, logger)

    assert "abc123" not in caplog.text
    assert not_json_line not in caplog.text
    assert json_without_detector not in caplog.text
    assert caplog.text.count(UNRECOGNIZED_OUTPUT_MARKER) == 2


def test_logs_privacy_check_redacts_non_object_json_lines(
    tmp_path: Path,
    logger: logging.Logger,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A line that parses as valid JSON but isn't an object (e.g. a bare string or array)
    must be treated as unrecognized and replaced with the marker, even if it happens to
    contain the substring "DetectorName" -- it must not be mistaken for a finding object
    and crash while summarizing it (regression test: json.loads() can return any JSON
    value, not just a dict, so "DetectorName" not in data is true for such values too).
    """
    log_file = tmp_path / "unit-target-0.log"
    log_file.write_text("some log line")

    json_string_line = json.dumps("a line mentioning DetectorName but not an object")
    json_array_line = json.dumps(["DetectorName", "PrivateKey", "abc123"])
    stdout = f"{json_string_line}\n{json_array_line}\n"

    version_check = MagicMock(returncode=0)
    scan_result = MagicMock(returncode=0, stdout=stdout, stderr="")

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:2] == ["trufflehog", "--version"]:
            return version_check
        return scan_result

    monkeypatch.setattr(subprocess, "run", fake_run)

    caplog.clear()
    with caplog.at_level(logging.INFO):
        test_logs_privacy_check(tmp_path, logger)

    assert "abc123" not in caplog.text
    assert json_string_line not in caplog.text
    assert json_array_line not in caplog.text
    assert caplog.text.count(UNRECOGNIZED_OUTPUT_MARKER) == 2
