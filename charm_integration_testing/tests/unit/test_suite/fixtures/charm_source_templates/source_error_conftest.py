import logging
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest
import requests
from test_suite.fixtures.charm_source import target_charm_source

from bundle_builder_x import (
    ApplicationReleaseDiagnostic,
    BundleBuildFailureDiagnostic,
    BundleBuildFailureKind,
    CharmReleaseNotFoundException,
    UncompletableBundleError,
)

__all__ = ["target_charm_source"]

STAGE = os.environ["CHARM_SOURCE_ERROR_STAGE"]
MESSAGE = os.environ["CHARM_SOURCE_ERROR_MESSAGE"]


@pytest.fixture(scope="session")
def logger() -> logging.Logger:
    return logging.getLogger("charmlint-resolution")


class GitStub:
    def checkout(self, *args: object) -> NoReturn:
        raise RuntimeError("Checkout failed")


@pytest.fixture(scope="session")
def git_client() -> GitStub:
    return GitStub()


@pytest.fixture(scope="session")
def target_resolved_charm() -> SimpleNamespace:
    if STAGE == "resolution":
        raise requests.HTTPError("Charmhub unavailable")
    if STAGE == "invalid-bundle":
        raise ValueError("Invalid saved bundle")
    if STAGE in ("solver-timeout", "optimization", "mixed-diagnostics"):
        kind = (
            BundleBuildFailureKind.OPTIMIZATION_UNSATISFIABLE
            if STAGE == "optimization"
            else BundleBuildFailureKind.SOLVER_TIMEOUT
        )
        diagnostics: list[BundleBuildFailureDiagnostic | ApplicationReleaseDiagnostic] = [
            BundleBuildFailureDiagnostic(kind=kind, detail=MESSAGE)
        ]
        if STAGE == "mixed-diagnostics":
            diagnostics.append(
                ApplicationReleaseDiagnostic(
                    application="target",
                    charm_name="example",
                    model="model",
                    error=CharmReleaseNotFoundException("Missing release"),
                )
            )
        raise UncompletableBundleError(diagnostics)
    return SimpleNamespace(name="example", revision=1)


class JujuStub:
    def download_charm(self, name: str, revision: int, destination: Path) -> None:
        if STAGE == "download":
            raise CharmReleaseNotFoundException("Download failed")
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("metadata.yaml", "name: example\nsource: https://example.com/repo")
            if STAGE == "checkout":
                archive.writestr("version", "abcdef0")


@pytest.fixture(scope="session")
def juju_client() -> JujuStub:
    return JujuStub()
