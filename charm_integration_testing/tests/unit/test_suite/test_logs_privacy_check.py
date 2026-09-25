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


def test_logs_privacy_check_scans_a_redacted_copy_and_tolerates_bad_bytes(
    tmp_path: Path,
    logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TruffleHog is invoked against a redacted copy of the log directory (see
    ``log_redaction.prepare_redacted_scan_dir``), not the log directory itself, so that
    known, non-sensitive ephemeral Juju bootstrap keys don't trigger false positives
    (issue #1033) while every other file and archive member -- including any real
    secret -- is still fully scanned.

    Also verifies the subprocess call tolerates non-UTF-8 bytes in TruffleHog's own
    stdout (e.g. leftover binary content it decoded from a scanned archive) instead of
    raising ``UnicodeDecodeError``, which is what issue #664 reported.
    """
    log_file = tmp_path / "juju-controller.log"
    log_file.write_text("nothing interesting here")

    version_check = MagicMock(returncode=0)
    scan_result = MagicMock(returncode=0, stdout=json.dumps({"level": "info-0", "msg": "no secrets found"}), stderr="")
    calls: list[list[str]] = []
    scanned_log_contents: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        calls.append(cmd)
        if cmd[:2] == ["trufflehog", "--version"]:
            return version_check
        assert kwargs.get("errors") == "replace", "must tolerate non-UTF-8 bytes in TruffleHog output"
        # Inspect the scan target while the temporary directory still exists.
        scanned_log_contents.append((Path(cmd[2]) / "juju-controller.log").read_text())
        return scan_result

    monkeypatch.setattr(subprocess, "run", fake_run)

    test_logs_privacy_check(tmp_path, logger)

    scan_cmd = calls[-1]
    assert scan_cmd[:2] == ["trufflehog", "filesystem"]
    assert Path(scan_cmd[2]) != tmp_path, "must scan a copy, not the original log directory"
    assert scan_cmd[3:] == ["--no-update", "--json", "--fail"]
    # The redacted copy must still contain the original (unredacted, since it has no
    # matching fields) file content -- nothing is silently dropped from the scan.
    assert scanned_log_contents == ["nothing interesting here"]


def test_logs_privacy_check_redacts_juju_bootstrap_keys_before_scanning(
    tmp_path: Path,
    logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for issue #1033: describe-controller-configmap.txt-style content
    with Juju's own ephemeral bootstrap private keys must be redacted before TruffleHog
    ever scans it, so it doesn't trigger a false-positive PrivateKey finding.
    """
    configmap_file = tmp_path / "describe-controller-configmap.txt"
    configmap_file.write_text(
        "controller-agent.conf:\n----\ncontrollerkey: |\n  totally-fake-controller-key-body-line\n"
    )

    version_check = MagicMock(returncode=0)
    scan_result = MagicMock(returncode=0, stdout="", stderr="")
    scanned_configmap_contents: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:2] == ["trufflehog", "--version"]:
            return version_check
        # Inspect the scan target while the temporary directory still exists.
        scanned_configmap_contents.append((Path(cmd[2]) / "describe-controller-configmap.txt").read_text())
        return scan_result

    monkeypatch.setattr(subprocess, "run", fake_run)

    test_logs_privacy_check(tmp_path, logger)

    (scanned_text,) = scanned_configmap_contents
    assert "totally-fake-controller-key-body-line" not in scanned_text
    assert "controllerkey: [REDACTED" in scanned_text


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


def test_logs_privacy_check_handles_malformed_nested_metadata(
    tmp_path: Path,
    logger: logging.Logger,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recognized finding object (has ``DetectorName``) with malformed/unexpected nested
    ``SourceMetadata`` must still be summarized without raising, falling back to placeholder
    location values instead of crashing. dict.get(key, default) only applies the default
    when the key is absent, not when it's present but null/wrong-typed, so each nesting
    level must be validated before being descended into.
    """
    log_file = tmp_path / "unit-target-0.log"
    log_file.write_text("some log line")

    secret_value = "-----BEGIN PRIVATE KEY-----\nTOTALLY-SECRET-KEY-MATERIAL\n-----END PRIVATE KEY-----"
    findings = [
        {"DetectorName": "PrivateKey", "Raw": secret_value, "Redacted": "redacted-1", "SourceMetadata": None},
        {"DetectorName": "PrivateKey", "Raw": secret_value, "Redacted": "redacted-2", "SourceMetadata": {"Data": None}},
        {
            "DetectorName": "PrivateKey",
            "Raw": secret_value,
            "Redacted": "redacted-3",
            "SourceMetadata": {"Data": {"Filesystem": "not-a-dict"}},
        },
    ]
    stdout = "\n".join(json.dumps(finding) for finding in findings) + "\n"

    version_check = MagicMock(returncode=0)
    scan_result = MagicMock(returncode=183, stdout=stdout, stderr="")

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
    assert "Detector=PrivateKey" in caplog.text
    assert "unknown file:?" in caplog.text
