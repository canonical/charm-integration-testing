# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import requests
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


@pytest.mark.parametrize("advertised_refs", [False, True], ids=["shallow", "advertised-refs"])
def test_fetch_retries_transient_failure(
    advertised_refs: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # GIVEN a fetch that fails once with a connection reset
    git = GitClient()
    calls: list[tuple[str, ...]] = []
    delays: list[float] = []
    monkeypatch.setattr("tenacity.nap.time.sleep", delays.append)

    def run(*args: str) -> str:
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(128, args, stderr="error: RPC failed; curl 56 Connection reset by peer")
        return ""

    monkeypatch.setattr(git, "_run", run)

    # WHEN fetching the source again
    if advertised_refs:
        git._fetch_advertised_refs("https://example.com/repo", tmp_path)
    else:
        git._fetch("https://example.com/repo", "abcdef0", tmp_path)

    # THEN only the failed fetch is repeated and the retry is visible
    assert calls == [calls[0], calls[0]]
    assert delays == [1]
    assert "Retrying" in caplog.text


@dataclass(frozen=True)
class FetchFailure:
    stderr: str
    attempts: int


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(
            FetchFailure("fatal: unable to access repository: Could not resolve host: example.com", 3), id="dns"
        ),
        pytest.param(FetchFailure("fatal: The requested URL returned error: 503", 3), id="server-error"),
        pytest.param(FetchFailure("fatal: The requested URL returned error: 429", 3), id="rate-limit"),
        pytest.param(FetchFailure("fatal: Authentication failed", 1), id="authentication"),
        pytest.param(FetchFailure("fatal: The requested URL returned error: 403", 1), id="forbidden"),
        pytest.param(FetchFailure("fatal: couldn't find remote ref missing", 1), id="missing-ref"),
        pytest.param(FetchFailure("fatal: SSL certificate problem: certificate has expired", 1), id="certificate"),
    ],
)
def test_fetch_preserves_failure(params: FetchFailure, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # GIVEN a fetch that always fails
    git = GitClient()
    error = subprocess.CalledProcessError(128, ["git", "fetch"], stderr=params.stderr)
    attempts = 0
    delays: list[float] = []
    monkeypatch.setattr("tenacity.nap.time.sleep", delays.append)

    def run(*args: str) -> str:
        nonlocal attempts
        attempts += 1
        raise error

    monkeypatch.setattr(git, "_run", run)

    # WHEN fetching the source
    with pytest.raises(subprocess.CalledProcessError) as raised:
        git._fetch("https://example.com/repo", "abcdef0", tmp_path)

    # THEN retries are bounded, permanent failures are immediate, and the original error survives
    assert raised.value is error
    assert attempts == params.attempts
    assert delays == [1, 2][: params.attempts - 1]


def test_regular_git_commands_are_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    # GIVEN a regular Git command reporting an otherwise retryable failure
    git = GitClient()
    attempts = 0

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        return subprocess.CompletedProcess(["git", "push"], 128, "", "error: RPC failed; curl 56 Connection reset")

    monkeypatch.setattr("utils.git.subprocess_run", run)

    # WHEN invoking the general-purpose command API
    with pytest.raises(subprocess.CalledProcessError):
        git.run("push")

    # THEN potentially mutating commands are not repeated
    assert attempts == 1


@dataclass(frozen=True)
class LookupFailure:
    error: requests.RequestException
    attempts: int


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(LookupFailure(requests.Timeout("Timed out"), 3), id="timeout"),
        pytest.param(LookupFailure(requests.ConnectionError("Connection reset"), 3), id="connection"),
        pytest.param(LookupFailure(_http_error(429), 3), id="rate-limit"),
        pytest.param(LookupFailure(_http_error(503), 3), id="server-error"),
        pytest.param(LookupFailure(_http_error(401), 1), id="authentication"),
        pytest.param(LookupFailure(_http_error(403), 1), id="forbidden"),
        pytest.param(LookupFailure(_http_error(404), 1), id="missing-commit"),
        pytest.param(LookupFailure(requests.exceptions.SSLError("Invalid certificate"), 1), id="certificate"),
    ],
)
def test_github_lookup_preserves_failure(params: LookupFailure, monkeypatch: pytest.MonkeyPatch) -> None:
    # GIVEN a GitHub lookup that always fails
    attempts = 0
    delays: list[float] = []
    monkeypatch.setattr("tenacity.nap.time.sleep", delays.append)

    def get(*args: object, **kwargs: object) -> requests.Response:
        nonlocal attempts
        attempts += 1
        raise params.error

    monkeypatch.setattr(requests, "get", get)

    # WHEN resolving an abbreviated commit
    with pytest.raises(requests.RequestException) as raised:
        GitClient()._resolve_github_commit("https://github.com/example/repo", "abcdef0")

    # THEN only transient errors retry and the original exception is propagated
    assert raised.value is params.error
    assert attempts == params.attempts
    assert delays == [1, 2][: params.attempts - 1]


def test_github_lookup_recovers_after_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # GIVEN GitHub temporarily returning a server error
    attempts = 0
    delays: list[float] = []
    monkeypatch.setattr("tenacity.nap.time.sleep", delays.append)

    def get(*args: object, **kwargs: object) -> requests.Response:
        nonlocal attempts
        attempts += 1
        response = requests.Response()
        response.status_code = 503 if attempts == 1 else 200
        response._content = b'{"sha": "abcdef0123456789012345678901234567890123"}'
        return response

    monkeypatch.setattr(requests, "get", get)

    # WHEN resolving an abbreviated commit
    commit = GitClient()._resolve_github_commit("https://github.com/example/repo", "abcdef0")

    # THEN the matching SHA is returned without changing the requested revision
    assert commit == "abcdef0123456789012345678901234567890123"
    assert attempts == 2
    assert delays == [1]
