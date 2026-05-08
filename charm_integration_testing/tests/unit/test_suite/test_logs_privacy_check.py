# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from test_suite.test_logs_privacy_check import test_logs_privacy_check


def test_logs_privacy_check_with_no_controllers(
    caplog: pytest.LogCaptureFixture,
    logger: logging.Logger,
) -> None:
    """Test privacy check handles the case when logs directory is empty.

    This test verifies that the privacy check is skipped gracefully when
    the log directory is empty (simulating the case where no controllers
    collected logs).
    """
    # Mock log_dir to return a mock path with no contents
    mock_logs_dir = MagicMock(spec=Path)
    mock_logs_dir.iterdir.side_effect = StopIteration

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        # Call the actual privacy check function with empty logs dir
        test_logs_privacy_check(mock_logs_dir, logger)

    # Verify the test was skipped
    assert "log-dir parameter not provided" in caplog.text
