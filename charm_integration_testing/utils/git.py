# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import os
import re
import shutil
from pathlib import Path
from subprocess import PIPE, CalledProcessError, CompletedProcess  # nosec B404
from subprocess import run as subprocess_run  # nosec B404
from urllib.parse import urlsplit

import requests


class GitClient:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger
        executable = shutil.which("git")
        if executable is None:
            raise FileNotFoundError("Git is required. Install Git and ensure `git` is on PATH.")
        self.executable = executable

    def run(self, *args: str, check: bool = True) -> CompletedProcess[str]:
        """Run Git with the caller's configuration."""
        return self._execute(args, check=check)

    def _execute(
        self, args: tuple[str, ...], *, check: bool = True, env: dict[str, str] | None = None
    ) -> CompletedProcess[str]:
        result = subprocess_run(  # nosec B603, B607
            [self.executable, *args],
            stdout=PIPE,
            stderr=PIPE,
            text=True,
            check=False,
            env=env,
        )
        if self.logger:
            if result.stdout:
                self.logger.info("Git output:\n%s", result.stdout)
            if result.stderr:
                self.logger.info("Git stderr:\n%s", result.stderr)
        if check:
            result.check_returncode()
        return result

    def checkout(self, repository: str, revision: str, destination: Path) -> str:
        if self.logger:
            self.logger.info("Checking out %s at revision %s.", repository, revision)
        is_tag = revision.startswith("refs/tags/")
        is_github_commit = urlsplit(repository).hostname == "github.com" and not is_tag
        if is_github_commit:
            revision = revision if len(revision) == 40 else self._resolve_github_commit(repository, revision)
        if is_tag or is_github_commit:
            self._run("init", "--quiet", str(destination))
            self._fetch(repository, revision, destination)
            commit = self._run("-C", str(destination), "rev-parse", "--verify", "FETCH_HEAD^{commit}")
        else:
            self._run("init", "--quiet", str(destination))
            try:
                self._fetch(repository, revision, destination)
                commit = self._run("-C", str(destination), "rev-parse", "--verify", "FETCH_HEAD^{commit}")
            except CalledProcessError:
                if re.fullmatch(r"[0-9a-f]{7,39}", revision):
                    # Abbreviated SHAs are not fetchable as refs; filter out blobs while scanning advertised history.
                    self._run(
                        "-C",
                        str(destination),
                        "fetch",
                        "--filter=blob:none",
                        "--no-tags",
                        "--",
                        repository,
                        "+refs/*:refs/cit-source/*",
                    )
                    commit = self._run("-C", str(destination), "rev-parse", "--verify", f"{revision}^{{commit}}")
                else:
                    raise
        self._run("-C", str(destination), "checkout", "--detach", commit)
        return commit

    def _fetch(self, repository: str, revision: str, destination: Path) -> None:
        self._run("-C", str(destination), "fetch", "--depth=1", "--no-tags", "--", repository, revision)

    def _run(self, *args: str) -> str:
        """Run a source checkout command without using the caller's global Git config."""
        return self._execute(
            ("-c", "credential.helper=", "-c", f"core.hooksPath={os.devnull}", *args),
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"},
        ).stdout.strip()

    def _resolve_github_commit(self, repository: str, revision: str) -> str:
        path = urlsplit(repository).path.strip("/").removesuffix(".git")
        response = requests.get(
            f"https://api.github.com/repos/{path}/commits/{revision}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "charm-integration-testing"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        commit = data.get("sha") if isinstance(data, dict) else None
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit) or not commit.startswith(revision):
            raise ValueError("GitHub did not resolve the packed version to a matching full commit SHA.")
        return commit
