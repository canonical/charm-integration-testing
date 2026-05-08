# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
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
