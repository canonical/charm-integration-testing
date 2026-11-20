# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import subprocess
from pathlib import Path

import pytest
from utils.git import GitClient


def test_source_checkout_isolates_git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # GIVEN a caller's Git configuration with a custom repository template
    template = tmp_path / "template"
    template.mkdir()
    (template / "marker").touch()
    config = tmp_path / "gitconfig"
    config.write_text(f'[init]\n\ttemplateDir = "{template}"\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    git = GitClient()
    repository = tmp_path / "repository"

    # WHEN running regular Git commands and checking out a source repository
    git.run("init", str(repository))
    git.run(
        "-C",
        str(repository),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "Original",
    )
    commit = git.run("-C", str(repository), "rev-parse", "HEAD").stdout.strip()
    destination = tmp_path / "source"
    assert git.checkout(repository.as_uri(), commit, destination) == commit

    # THEN regular commands honor the caller's configuration, but source checkout does not
    assert (repository / ".git" / "marker").is_file()
    assert not (destination / ".git" / "marker").exists()


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
