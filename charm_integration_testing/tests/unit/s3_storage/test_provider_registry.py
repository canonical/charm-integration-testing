# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests
from s3_storage import S3Bucket, S3Provider, S3ProviderRegistry, S3StorageHandle, StaticS3Provider, SwiftS3Provider
from s3_storage.configuration import parse_s3_config, provider_from_config

STATIC_CONFIG = (
    "S3_ENDPOINT=http://s3.test:8333\n"
    "S3_ACCESS_KEY=application-access\n"
    "S3_SECRET_KEY=application-secret\n"
    "S3_BUCKET_NAMESPACE=test\n"
)


@dataclass
class ProviderStub(S3Provider):
    identity: str
    cleanup_calls: int = 0
    fail_cleanup: bool = False

    @property
    def handle(self) -> S3StorageHandle:
        return S3StorageHandle("test", self.identity)

    @property
    def endpoint(self) -> str:
        return "http://s3.test:8333"

    @property
    def region(self) -> str:
        return "us-east-1"

    def new_bucket(self) -> S3Bucket:
        raise NotImplementedError

    def cleanup_stale(self, age: timedelta, now: datetime | None = None) -> list[str]:
        raise NotImplementedError

    def cleanup_pending_resources(self) -> None:
        self.cleanup_calls += 1
        if self.fail_cleanup:
            raise RuntimeError("Cleanup failed")


@dataclass
class ProviderFactory:
    providers: dict[Path, ProviderStub]
    calls: list[tuple[Path, requests.Session]] = field(default_factory=list)

    def __call__(self, path: Path, session: requests.Session) -> S3Provider:
        self.calls.append((path, session))
        return self.providers[path]


def test_provider_is_created_lazily_and_reused_by_cloud() -> None:
    # GIVEN a registry whose cloud has not been accessed
    config = Path("target")
    provider = ProviderStub("target")
    factory = ProviderFactory({config: provider})
    with requests.Session() as session:
        registry = S3ProviderRegistry({"target-cloud": config}, session, factory)
        assert factory.calls == []

        # WHEN the same cloud is requested twice
        assert registry.provider_for_cloud("target-cloud") is provider
        assert registry.provider_for_cloud("target-cloud") is provider

        # THEN provider creation only happens once
        assert factory.calls == [(config, session)]


def test_providers_are_deduplicated_by_identity_not_path() -> None:
    factory = ProviderFactory({Path("first"): ProviderStub("shared"), Path("second"): ProviderStub("shared")})
    with requests.Session() as session:
        registry = S3ProviderRegistry(
            {"target-cloud": Path("first"), "neighbor-cloud": Path("second")}, session, factory
        )

        assert registry.provider_for_cloud("target-cloud") is registry.provider_for_cloud("neighbor-cloud")
        registry.cleanup_pending_resources()

    assert factory.providers[Path("first")].cleanup_calls == 1
    assert factory.providers[Path("second")].cleanup_calls == 0


def test_cleanup_attempts_every_used_provider_and_surfaces_failures() -> None:
    first = ProviderStub("first", fail_cleanup=True)
    second = ProviderStub("second")
    unused = ProviderStub("unused")
    factory = ProviderFactory({Path("first"): first, Path("second"): second, Path("unused"): unused})
    with requests.Session() as session:
        registry = S3ProviderRegistry({str(path): path for path in factory.providers}, session, factory)
        registry.provider_for_cloud("first")
        registry.provider_for_cloud("second")

        with pytest.raises(RuntimeError, match="Pending S3 cleanup failed for 1 provider") as error:
            registry.cleanup_pending_resources()

    assert str(error.value.__cause__) == "Cleanup failed"
    assert first.cleanup_calls == second.cleanup_calls == 1
    assert unused.cleanup_calls == 0


def test_missing_cloud_configuration_fails_without_creating_a_provider() -> None:
    factory = ProviderFactory({})
    with requests.Session() as session:
        registry = S3ProviderRegistry({}, session, factory)
        with pytest.raises(ValueError, match="No S3 configuration for Juju cloud 'new-cloud'"):
            registry.provider_for_cloud("new-cloud")
    assert factory.calls == []


def test_failed_provider_creation_is_not_cached(tmp_path: Path) -> None:
    config = tmp_path / "s3.conf"
    config.write_text("S3_ENDPOINT=http://s3.test\n")
    with requests.Session() as session:
        registry = S3ProviderRegistry({"local": config}, session)
        with pytest.raises(ValueError, match="Missing S3 credential variables"):
            registry.provider_for_cloud("local")
        config.write_text(STATIC_CONFIG)
        assert isinstance(registry.provider_for_cloud("local"), StaticS3Provider)


@dataclass(frozen=True)
class InvalidConfigParams:
    content: str
    error: str


@pytest.mark.parametrize(
    "params",
    [
        InvalidConfigParams("export S3_SECRET_KEY=not-logged", "Invalid S3 config entry on line 1"),
        InvalidConfigParams("UNKNOWN=not-logged", "Invalid S3 config entry on line 1"),
        InvalidConfigParams("S3_SECRET_KEY=not-logged\nS3_SECRET_KEY=also-secret", "Duplicate S3 config entry"),
    ],
    ids=["shell-export", "unknown-variable", "duplicate-variable"],
)
def test_invalid_config_does_not_expose_values(params: InvalidConfigParams) -> None:
    with pytest.raises(ValueError, match=params.error) as error:
        parse_s3_config(params.content)
    assert "not-logged" not in str(error.value)
    assert "also-secret" not in str(error.value)


def test_config_parser_keeps_equals_and_treats_shell_syntax_as_literal() -> None:
    assert parse_s3_config("# comment\n\nS3_SECRET_KEY=$(not-executed)=value\n") == {
        "S3_SECRET_KEY": "$(not-executed)=value"
    }


def test_static_configuration_ignores_ambient_swift_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "s3.conf"
    config.write_text(STATIC_CONFIG)
    monkeypatch.setenv("S3_OS_STORAGE_URL", "https://unrelated.test/swift/v1")
    with requests.Session() as session:
        provider = provider_from_config(config, session)
    assert isinstance(provider, StaticS3Provider)
    assert provider.endpoint == "http://s3.test:8333"
    assert provider.handle.namespace == "test"


def test_swift_config_supports_explicit_endpoint_and_region(tmp_path: Path) -> None:
    config = tmp_path / "s3.conf"
    config.write_text(
        "S3_OS_AUTH_URL=https://identity.test/v3\n"
        "S3_OS_USERNAME=user\n"
        "S3_OS_PASSWORD=test-password\n"
        "S3_OS_USER_DOMAIN_NAME=users\n"
        "S3_OS_PROJECT_NAME=project\n"
        "S3_OS_PROJECT_DOMAIN_NAME=projects\n"
        "S3_OS_STORAGE_URL=https://storage.test/swift/v1/AUTH_project\n"
        "S3_CLEANUP_ACCESS_KEY=cleanup-access\n"
        "S3_CLEANUP_SECRET_KEY=cleanup-secret\n"
        "S3_ENDPOINT=https://explicit.test\n"
        "S3_REGION=eu-west-1\n"
    )
    with requests.Session() as session:
        provider = provider_from_config(config, session)
    assert isinstance(provider, SwiftS3Provider)
    assert provider.endpoint == "https://explicit.test"
    assert provider.region == "eu-west-1"


def test_equivalent_files_reuse_provider_and_distinct_configs_do_not(tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ("first", "equivalent", "different")]
    for path in paths[:2]:
        path.write_text(STATIC_CONFIG)
    paths[2].write_text(STATIC_CONFIG.replace("S3_BUCKET_NAMESPACE=test", "S3_BUCKET_NAMESPACE=other"))
    with requests.Session() as session:
        registry = S3ProviderRegistry(dict(zip(("first", "equivalent", "different"), paths, strict=True)), session)
        first = registry.provider_for_cloud("first")
        assert registry.provider_for_cloud("equivalent") is first
        assert registry.provider_for_cloud("different") is not first


@pytest.mark.parametrize("content", ["", "# no credentials\n", "S3_REGION=us-east-1\n"])
def test_config_must_select_a_provider(tmp_path: Path, content: str) -> None:
    config = tmp_path / "s3.conf"
    config.write_text(content)
    with requests.Session() as session, pytest.raises(ValueError, match="No S3 provider configured"):
        provider_from_config(config, session)


def test_missing_config_file_is_reported(tmp_path: Path) -> None:
    with requests.Session() as session, pytest.raises(FileNotFoundError):
        S3ProviderRegistry({"local": tmp_path / "missing"}, session).provider_for_cloud("local")
