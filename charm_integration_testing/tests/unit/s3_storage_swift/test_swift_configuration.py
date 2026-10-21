# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import subprocess
import sys
from pathlib import Path

import pytest
import requests
from s3_storage import StaticS3Provider
from s3_storage.configuration import provider_from_config as static_provider_from_config
from s3_storage_swift import SwiftS3Provider
from s3_storage_swift.configuration import provider_from_config

STATIC_CONFIG = (
    "S3_ENDPOINT=http://s3.test:8333\nS3_ACCESS_KEY=application-access\nS3_SECRET_KEY=application-secret\n"
    "S3_BUCKET_NAMESPACE=test\n"
)
SWIFT_CONFIG = (
    "S3_OS_AUTH_URL=https://identity.test/v3\n"
    "S3_OS_USERNAME=user\nS3_OS_PASSWORD=test-password\nS3_OS_USER_DOMAIN_NAME=users\n"
    "S3_OS_PROJECT_NAME=project\nS3_OS_PROJECT_DOMAIN_NAME=projects\n"
    "S3_OS_STORAGE_URL=https://storage.test/swift/v1/AUTH_project\n"
    "S3_CLEANUP_ACCESS_KEY=cleanup-access\nS3_CLEANUP_SECRET_KEY=cleanup-secret\n"
)


def test_generic_package_import_does_not_load_swift() -> None:
    # GIVEN an interpreter where the sibling implementation cannot be imported
    code = """
import importlib.abc
import sys

class BlockSwift(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "s3_storage_swift" or fullname.startswith("s3_storage_swift."):
            raise ImportError("Swift is unavailable")

sys.meta_path.insert(0, BlockSwift())
import s3_storage
import s3_storage.configuration
import s3_storage.environment
"""
    # WHEN importing generic modules, THEN the sibling package is unnecessary
    subprocess.run([sys.executable, "-c", code], check=True)


def test_generic_loader_does_not_accept_swift_keys(tmp_path: Path) -> None:
    # GIVEN a Swift file passed to the static-only loader
    path = tmp_path / "s3.conf"
    path.write_text(SWIFT_CONFIG)
    # WHEN loading it, THEN Swift support must be explicitly selected by the caller
    with pytest.raises(ValueError, match="Invalid S3 config entry on line 1"):
        static_provider_from_config(path)


def test_static_configuration_ignores_ambient_swift_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "s3.conf"
    path.write_text(STATIC_CONFIG)
    monkeypatch.setenv("S3_OS_STORAGE_URL", "https://unrelated.test/swift/v1")
    with requests.Session() as session:
        provider = provider_from_config(path, session)
    assert isinstance(provider, StaticS3Provider)
    assert provider.endpoint == "http://s3.test:8333"
    assert provider.handle.namespace == "test"


def test_swift_config_supports_explicit_endpoint_and_region(tmp_path: Path) -> None:
    path = tmp_path / "s3.conf"
    path.write_text(SWIFT_CONFIG + "S3_ENDPOINT=https://explicit.test\nS3_REGION=eu-west-1\n")
    with requests.Session() as session:
        provider = provider_from_config(path, session)
    assert isinstance(provider, SwiftS3Provider)
    assert provider.endpoint == "https://explicit.test"
    assert provider.region == "eu-west-1"


@pytest.mark.parametrize(
    "extra",
    [
        "S3_ACCESS_KEY=not-logged\n",
        "S3_SECRET_KEY=not-logged\n",
        "S3_ACCESS_KEY=not-logged\nS3_SECRET_KEY=also-secret\n",
        "S3_ACCESS_KEY=\n",
        "S3_SECRET_KEY=\n",
    ],
    ids=["partial-access-key", "partial-secret-key", "complete-static-pair", "empty-access-key", "empty-secret-key"],
)
def test_swift_config_rejects_static_credentials(tmp_path: Path, extra: str) -> None:
    path = tmp_path / "s3.conf"
    path.write_text(SWIFT_CONFIG + extra)
    with requests.Session() as session, pytest.raises(ValueError, match="Cannot mix static and Swift") as error:
        provider_from_config(path, session)
    assert "not-logged" not in str(error.value)
    assert "also-secret" not in str(error.value)


@pytest.mark.parametrize(
    "entry",
    ["S3_OS_USERNAME=not-logged\n", "S3_OS_USERNAME=\n", "S3_OS_STORAGE_URL=\n"],
    ids=["partial-swift", "empty-username", "empty-storage-url"],
)
def test_partial_swift_config_does_not_fall_back_to_static(tmp_path: Path, entry: str) -> None:
    path = tmp_path / "s3.conf"
    path.write_text(STATIC_CONFIG + entry)
    with requests.Session() as session, pytest.raises(ValueError, match="Cannot mix static and Swift"):
        provider_from_config(path, session)


def test_swift_config_without_storage_url_reports_missing_selector(tmp_path: Path) -> None:
    path = tmp_path / "s3.conf"
    path.write_text("S3_OS_USERNAME=not-logged\n")
    with (
        requests.Session() as session,
        pytest.raises(ValueError, match="Missing Swift credential variable: S3_OS_STORAGE_URL"),
    ):
        provider_from_config(path, session)
