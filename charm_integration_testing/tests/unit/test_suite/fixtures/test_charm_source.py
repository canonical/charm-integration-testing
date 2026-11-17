# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path
from subprocess import CalledProcessError

import pytest
import requests
from test_suite.fixtures import charm_source as module
from utils.git import GitClient


@pytest.mark.parametrize(
    ("filename", "metadata"),
    [
        pytest.param("metadata.yaml", "source: https://example.com/charm.git", id="legacy"),
        pytest.param("charmcraft.yaml", "links:\n  source: https://example.com/charm.git", id="modern"),
        pytest.param("charmcraft.yaml", "links:\n  source: [https://example.com/charm.git]", id="modern-list"),
    ],
)
def test_read_source(tmp_path: Path, filename: str, metadata: str) -> None:
    # GIVEN a packed charm that records its source and build version
    with zipfile.ZipFile(tmp_path / "target.charm", "w") as archive:
        archive.writestr(filename, metadata)
        archive.writestr("version", "v1.0-2-gabcdef0\n")

        # WHEN reading the provenance without extracting the archive
        source = module._read_source(archive)

    # THEN the repository and packed version are preserved
    assert source == module._CharmSource("https://example.com/charm.git", "v1.0-2-gabcdef0")
    assert not (tmp_path / filename).exists()


def test_read_source_falls_back_to_legacy_metadata(tmp_path: Path) -> None:
    # GIVEN a build-only charmcraft.yaml and legacy charm metadata
    with zipfile.ZipFile(tmp_path / "target.charm", "w") as archive:
        archive.writestr("charmcraft.yaml", "type: charm\nparts: {}")
        archive.writestr("metadata.yaml", "source: https://example.com/charm.git")
        archive.writestr("version", "abcdef0")

        # WHEN resolving the source
        source = module._read_source(archive)

    # THEN build-part sources are not mistaken for repository links
    assert source.repository == "https://example.com/charm.git"


@dataclass(frozen=True)
class InvalidSource:
    version: str | None
    metadata: str
    error: str


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(InvalidSource(None, "", "no version file"), id="missing-version"),
        pytest.param(InvalidSource("", "", "clean Git commit"), id="empty-version"),
        pytest.param(InvalidSource("abcdef0-dirty", "", "clean Git commit"), id="dirty-worktree"),
        pytest.param(InvalidSource("--upload-pack=bad", "", "clean Git commit"), id="invalid-ref"),
        pytest.param(InvalidSource("abcdef0", "{}", "no source repository"), id="missing-source"),
        pytest.param(
            InvalidSource("abcdef0", "source: [https://example.com/a, https://example.com/b]", "exactly one"),
            id="ambiguous-source",
        ),
        pytest.param(
            InvalidSource("abcdef0", "source: file:///tmp/repo", "HTTPS Git repository"),
            id="local-path",
        ),
        pytest.param(
            InvalidSource("abcdef0", "source: https://user:password@example.com/repo", "without credentials"),
            id="embedded-credentials",
        ),
        pytest.param(
            InvalidSource("abcdef0", "source: https://github.com/owner/repo/tree/main", "repository root"),
            id="branch-url",
        ),
    ],
)
def test_read_source_rejects_unusable_provenance(tmp_path: Path, params: InvalidSource) -> None:
    # GIVEN missing or unsupported source provenance
    with zipfile.ZipFile(tmp_path / "target.charm", "w") as archive:
        archive.writestr("metadata.yaml", params.metadata)
        if params.version is not None:
            archive.writestr("version", params.version)

        # WHEN resolving the source, THEN it fails instead of linting the packed files
        with pytest.raises(ValueError, match=params.error):
            module._read_source(archive)


@pytest.fixture
def repository(tmp_path: Path, logger: logging.Logger) -> tuple[Path, str]:
    path = tmp_path / "repository"
    git = GitClient(logger)
    git._run("init", "--quiet", str(path))
    (path / "metadata.yaml").write_text("name: example\n")
    (path / "tests" / "unit").mkdir(parents=True)
    test = path / "tests" / "unit" / "test_charm.py"
    test.write_text("original source\n")
    git._run("-C", str(path), "add", ".")
    git._run(
        "-C",
        str(path),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "Original",
    )
    commit = git._run("-C", str(path), "rev-parse", "HEAD")
    git._run("-C", str(path), "tag", "v1.0")
    test.write_text("newer unrelated source\n")
    git._run("-C", str(path), "add", ".")
    git._run(
        "-C",
        str(path),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "Newer",
    )
    return path, commit


@pytest.mark.parametrize("version_format", ["{}", "{:.7}", "v1.0-2-g{:.7}", "v1.0"])
def test_checkout_uses_packed_revision(
    repository: tuple[Path, str],
    version_format: str,
    tmp_path: Path,
    logger: logging.Logger,
) -> None:
    # GIVEN a repository whose branch has moved beyond the packed revision
    path, commit = repository
    source = module._CharmSource(str(path), version_format.format(commit))
    destination = tmp_path / "source"

    # WHEN checking out the packed version
    checked_out = GitClient(logger).checkout(source.repository, module._version_ref(source.version), destination)

    # THEN lint input includes source-only files at the original detached commit
    assert checked_out == commit
    git = GitClient(logger)
    assert git._run("-C", str(destination), "rev-parse", "HEAD") == commit
    assert (destination / "tests/unit/test_charm.py").read_text() == "original source\n"
    assert git._run("-C", str(destination), "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    module._verify_charm_root(destination, "example")


@pytest.mark.parametrize("use_tag", [True, False], ids=["tag", "commit"])
def test_checkout_fetches_ref_outside_default_branch(
    repository: tuple[Path, str], use_tag: bool, tmp_path: Path, logger: logging.Logger
) -> None:
    # GIVEN a tag or commit outside the repository's default branch
    path, _ = repository
    git = GitClient(logger)
    default_branch = git._run("-C", str(path), "branch", "--show-current")
    git._run("-C", str(path), "checkout", "--orphan", "release-only")
    test = path / "tests" / "unit" / "test_charm.py"
    test.write_text("release-only source\n")
    git._run("-C", str(path), "add", ".")
    git._run(
        "-C",
        str(path),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "Release-only",
    )
    release_commit = git._run("-C", str(path), "rev-parse", "HEAD")
    revision = release_commit
    if use_tag:
        git._run("-C", str(path), "tag", "release-only")
        revision = "refs/tags/release-only"
    git._run("-C", str(path), "checkout", default_branch)
    destination = tmp_path / "source"

    # WHEN checking out the packed revision
    checked_out = git.checkout(str(path), revision, destination)

    # THEN the explicit fetch finds the commit despite it not being on the default branch
    assert checked_out == release_commit
    assert (destination / "tests/unit/test_charm.py").read_text() == "release-only source\n"


def test_checkout_resolves_described_commit_from_advertised_ref(
    repository: tuple[Path, str], tmp_path: Path, logger: logging.Logger
) -> None:
    # GIVEN a described commit advertised only under a merge-request ref
    path, _ = repository
    git = GitClient(logger)
    default_branch = git._run("-C", str(path), "branch", "--show-current")
    git._run("-C", str(path), "checkout", "--orphan", "merge-request")
    (path / "tests/unit/test_charm.py").write_text("merge-request source\n")
    git._run("-C", str(path), "add", ".")
    git._run(
        "-C",
        str(path),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "Merge request",
    )
    commit = git._run("-C", str(path), "rev-parse", "HEAD")
    (path / "tests/unit/test_charm.py").write_text("newer merge-request source\n")
    git._run("-C", str(path), "add", ".")
    git._run(
        "-C",
        str(path),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "Newer merge request",
    )
    git._run("-C", str(path), "update-ref", "refs/merge-requests/1/merge", "HEAD")
    git._run("-C", str(path), "checkout", default_branch)
    git._run("-C", str(path), "branch", "-D", "merge-request")
    destination = tmp_path / "source"

    # WHEN resolving a non-GitHub git-describe version with real Git
    checked_out = git.checkout(path.as_uri(), module._version_ref(f"v1.0-2-g{commit[:7]}"), destination)

    # THEN the advertised ref supplies the exact packed commit, not the default branch
    assert checked_out == commit
    assert (destination / "tests/unit/test_charm.py").read_text() == "merge-request source\n"
    assert git._run("-C", str(destination), "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


def test_checkout_never_falls_back_to_branch(
    repository: tuple[Path, str], tmp_path: Path, logger: logging.Logger
) -> None:
    # GIVEN a version that names a branch but no tag
    path, _ = repository
    GitClient(logger)._run("-C", str(path), "branch", "release")
    source = module._CharmSource(str(path), "release")

    # WHEN checking out the version, THEN no current branch can substitute for a tag
    with pytest.raises(CalledProcessError):
        GitClient(logger).checkout(source.repository, module._version_ref(source.version), tmp_path / "source")


def test_github_merge_commit_outside_cloned_history(
    repository: tuple[Path, str],
    tmp_path: Path,
    logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GIVEN a GitHub version that must be resolved to a full SHA before fetching
    path, commit = repository
    repository_url = "https://github.com/example/charm.git"
    original_run = GitClient._run
    api_calls: list[str] = []
    fetches: list[tuple[str, ...]] = []

    def get_commit(url: str, *, timeout: int) -> requests.Response:
        api_calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"sha": commit}).encode()
        return response

    def local_git(git: GitClient, *args: str) -> str:
        if "fetch" in args:
            fetches.append(args)
        return original_run(git, *(str(path) if arg == repository_url else arg for arg in args))

    monkeypatch.setattr(requests, "get", get_commit)
    monkeypatch.setattr(GitClient, "_run", local_git)
    destination = tmp_path / "source"

    # WHEN preparing the source checkout using real Git against the local test repository
    checked_out = GitClient(logger).checkout(repository_url, commit[:7], destination)

    # THEN the API resolves the abbreviation and Git fetches that full commit, not a branch
    assert api_calls == [f"https://api.github.com/repos/example/charm/commits/{commit[:7]}"]
    assert len(fetches) == 1
    assert fetches[0][-2:] == (repository_url, commit)
    assert checked_out == commit
    assert (destination / "tests/unit/test_charm.py").read_text() == "original source\n"


@pytest.mark.parametrize(
    ("status", "data", "error"),
    [
        pytest.param(200, {"sha": "b" * 40}, ValueError, id="mismatched-sha"),
        pytest.param(200, {"sha": "abcdef0"}, ValueError, id="abbreviated-response"),
        pytest.param(404, {"message": "Not Found"}, requests.HTTPError, id="unresolvable-commit"),
    ],
)
def test_github_resolution_failure_is_not_ignored(
    status: int, data: dict[str, str], error: type[Exception], logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GIVEN an unresolved or inconsistent source commit
    def get_commit(url: str, *, timeout: int) -> requests.Response:
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(data).encode()
        return response

    monkeypatch.setattr(requests, "get", get_commit)

    # WHEN resolving the version, THEN the failure propagates
    with pytest.raises(error):
        GitClient(logger)._resolve_github_commit("https://github.com/example/charm", "abcdef0")


@pytest.mark.parametrize(
    ("files", "error"),
    [
        pytest.param({"metadata.yaml": "name: different"}, "expected 'example'", id="wrong-charm"),
        pytest.param({"subdir/metadata.yaml": "name: example"}, "no root charm metadata", id="monorepo"),
    ],
)
def test_checkout_must_contain_the_target_charm(tmp_path: Path, files: dict[str, str], error: str) -> None:
    # GIVEN a source checkout whose root does not match the charm
    for filename, content in files.items():
        path = tmp_path / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    # WHEN selecting the lint root, THEN an unrelated project cannot be used
    with pytest.raises(ValueError, match=error):
        module._verify_charm_root(tmp_path, "example")


def test_checkout_accepts_build_only_charmcraft(tmp_path: Path) -> None:
    # GIVEN build configuration separate from legacy charm metadata
    (tmp_path / "charmcraft.yaml").write_text("type: charm\nparts: {}")
    (tmp_path / "metadata.yaml").write_text("name: example")

    # WHEN verifying the source root, THEN the legacy name identifies the charm
    module._verify_charm_root(tmp_path, "example")
