# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import os
import re
from pathlib import Path
from subprocess import PIPE, STDOUT, CalledProcessError, run  # nosec B404
from urllib.parse import urlsplit

import requests


class GitClient:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def checkout(self, repository: str, revision: str, destination: Path) -> str:
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
            self._run("clone", "--no-checkout", "--", repository, str(destination))
            try:
                commit = self._run("-C", str(destination), "rev-parse", "--verify", f"{revision}^{{commit}}")
            except CalledProcessError:
                self._fetch(repository, revision, destination)
                commit = self._run("-C", str(destination), "rev-parse", "--verify", "FETCH_HEAD^{commit}")
        self._run("-C", str(destination), "checkout", "--detach", commit)
        return commit

    def _fetch(self, repository: str, revision: str, destination: Path) -> None:
        self._run("-C", str(destination), "fetch", "--depth=1", "--no-tags", "--", repository, revision)

    def _run(self, *args: str) -> str:
        result = run(  # nosec B603, B607
            ["git", "-c", "credential.helper=", "-c", f"core.hooksPath={os.devnull}", *args],
            stdout=PIPE,
            stderr=STDOUT,
            text=True,
            check=False,
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"},
        )
        if result.stdout:
            self.logger.info("Git output:\n%s", result.stdout)
        result.check_returncode()
        return result.stdout.strip()

    def _resolve_github_commit(self, repository: str, revision: str) -> str:
        path = urlsplit(repository).path.strip("/").removesuffix(".git")
        response = requests.get(f"https://api.github.com/repos/{path}/commits/{revision}", timeout=30)
        response.raise_for_status()
        data = response.json()
        commit = data.get("sha") if isinstance(data, dict) else None
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit) or not commit.startswith(revision):
            raise ValueError("GitHub did not resolve the packed version to a matching full commit SHA.")
        return commit
