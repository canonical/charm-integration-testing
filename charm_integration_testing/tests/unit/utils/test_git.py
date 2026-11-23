# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import subprocess
from pathlib import Path

import pytest
from utils.git import GitClient


def test_source_checkout_isolates_git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # GIVEN caller and system Git configurations with custom repository templates
    template = tmp_path / "template"
    template.mkdir()
    (template / "marker").touch()
    config = tmp_path / "gitconfig"
    config.write_text(f'[init]\n\ttemplateDir = "{template}"\n')
    system_template = tmp_path / "system-template"
    system_template.mkdir()
    (system_template / "system-marker").touch()
    system_config = tmp_path / "system-gitconfig"
    system_config.write_text(f'[init]\n\ttemplateDir = "{system_template}"\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))
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

    # THEN regular commands honor caller config, but source checkout ignores caller and system config
    assert (repository / ".git" / "marker").is_file()
    assert not (destination / ".git" / "marker").exists()
    assert not (destination / ".git" / "system-marker").exists()


def test_advertised_fetch_retries_without_unsupported_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git = GitClient()
    calls: list[tuple[str, ...]] = []

    def run(*args: str) -> str:
        calls.append(args)
        if "--filter=blob:none" in args:
            raise subprocess.CalledProcessError(1, args, stderr="fatal: server does not support filter")
        return ""

    monkeypatch.setattr(git, "_run", run)

    git._fetch_advertised_refs("https://example.com/repo", tmp_path / "source")

    fetches = [call for call in calls if "fetch" in call]
    assert len(fetches) == 2
    assert "--filter=blob:none" in fetches[0]
    assert "--filter=blob:none" not in fetches[1]


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
