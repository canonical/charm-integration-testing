# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

import pytest


@pytest.fixture(scope="session")
def logger() -> logging.Logger:
    """Provide a logger for unit tests."""
    return logging.getLogger(__name__)
