# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import subprocess

import pytest
from utils.git import GitClient


def test_run_returns_failed_process_when_check_is_false() -> None:
    # GIVEN a Git command that cannot resolve a revision
    git = GitClient()

    # WHEN running it without raising on failure
    result = git.run("rev-parse", "--verify", "missing-ref", check=False)

    # THEN both output streams and the non-zero return code are available
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr


def test_run_raises_when_check_is_true() -> None:
    # GIVEN a Git command that cannot resolve a revision
    git = GitClient()

    # WHEN running it with the default check behavior
    # THEN the failed process raises CalledProcessError
    with pytest.raises(subprocess.CalledProcessError):
        git.run("rev-parse", "--verify", "missing-ref")
