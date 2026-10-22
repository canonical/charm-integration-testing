# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from s3_storage import S3Bucket, S3Provider, S3ProviderRegistry, S3StorageHandle, StaticS3Provider
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
    namespace: str = "test"

    @property
    def handle(self) -> S3StorageHandle:
        return S3StorageHandle(self.namespace, self.identity)

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


@dataclass
class ProviderFactory:
    providers: dict[Path, ProviderStub]
    calls: list[Path] = field(default_factory=list)

    def __call__(self, path: Path) -> S3Provider:
        self.calls.append(path)
        return self.providers[path]


def test_provider_is_created_lazily_and_reused_by_cloud() -> None:
    # GIVEN a registry whose cloud has not been accessed
    config = Path("target")
    provider = ProviderStub("target")
    factory = ProviderFactory({config: provider})
    registry = S3ProviderRegistry({"target-cloud": config}, factory)
    assert factory.calls == []

    # WHEN the same cloud is requested twice
    assert registry.provider_for_cloud("target-cloud") is provider
    assert registry.provider_for_cloud("target-cloud") is provider

    # THEN provider creation only happens once
    assert factory.calls == [config]


def test_providers_are_deduplicated_by_identity_not_path() -> None:
    factory = ProviderFactory({Path("first"): ProviderStub("shared"), Path("second"): ProviderStub("shared")})
    registry = S3ProviderRegistry({"target-cloud": Path("first"), "neighbor-cloud": Path("second")}, factory)

    assert registry.provider_for_cloud("target-cloud") is registry.provider_for_cloud("neighbor-cloud")


def test_providers_with_the_same_resource_id_share_a_cache_entry() -> None:
    # GIVEN distinct handles whose effective resource identities are equal
    first = ProviderStub("shared", "first")
    second = ProviderStub("shared", "second")
    assert first.handle != second.handle
    assert first.handle.resource_id == second.handle.resource_id
    factory = ProviderFactory({Path("first"): first, Path("second"): second})
    registry = S3ProviderRegistry({"first-cloud": Path("first"), "second-cloud": Path("second")}, factory)

    # WHEN both clouds are resolved, THEN only one provider can reach resource registration
    assert registry.provider_for_cloud("first-cloud") is first
    assert registry.provider_for_cloud("second-cloud") is first


def test_equivalent_static_endpoint_files_reuse_the_same_provider(tmp_path: Path) -> None:
    # GIVEN file contents differing only in a normalized endpoint slash
    first = tmp_path / "first.conf"
    second = tmp_path / "second.conf"
    first.write_text(STATIC_CONFIG)
    second.write_text(STATIC_CONFIG.replace("http://s3.test:8333", "http://s3.test:8333/"))
    registry = S3ProviderRegistry({"first": first, "second": second})

    # WHEN both aliases are used, THEN the lazy registry reuses the effective provider
    assert registry.provider_for_cloud("first") is registry.provider_for_cloud("second")


def test_missing_cloud_configuration_fails_without_creating_a_provider() -> None:
    factory = ProviderFactory({})
    registry = S3ProviderRegistry({}, factory)
    with pytest.raises(ValueError, match="No S3 configuration for Juju cloud 'new-cloud'"):
        registry.provider_for_cloud("new-cloud")
    assert factory.calls == []


def test_failed_provider_creation_is_not_cached(tmp_path: Path) -> None:
    config = tmp_path / "s3.conf"
    config.write_text("S3_ENDPOINT=http://s3.test\n")
    registry = S3ProviderRegistry({"local": config})
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


def test_equivalent_files_reuse_provider_and_distinct_configs_do_not(tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ("first", "equivalent", "different")]
    for path in paths[:2]:
        path.write_text(STATIC_CONFIG)
    paths[2].write_text(STATIC_CONFIG.replace("S3_BUCKET_NAMESPACE=test", "S3_BUCKET_NAMESPACE=other"))
    registry = S3ProviderRegistry(dict(zip(("first", "equivalent", "different"), paths, strict=True)))
    first = registry.provider_for_cloud("first")
    assert registry.provider_for_cloud("equivalent") is first
    assert registry.provider_for_cloud("different") is not first


@pytest.mark.parametrize("content", ["", "# no credentials\n", "S3_REGION=us-east-1\n"])
def test_config_must_select_a_provider(tmp_path: Path, content: str) -> None:
    config = tmp_path / "s3.conf"
    config.write_text(content)
    with pytest.raises(ValueError, match="No S3 provider configured"):
        provider_from_config(config)


def test_missing_config_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        S3ProviderRegistry({"local": tmp_path / "missing"}).provider_for_cloud("local")


@pytest.mark.parametrize(
    "extra",
    [
        "S3_CLEANUP_ACCESS_KEY=\n",
        "S3_CLEANUP_SECRET_KEY=\n",
        "S3_CLEANUP_ACCESS_KEY=\nS3_CLEANUP_SECRET_KEY=\n",
        "S3_CLEANUP_ACCESS_KEY=not-logged\nS3_CLEANUP_SECRET_KEY=\n",
        "S3_CLEANUP_ACCESS_KEY=\nS3_CLEANUP_SECRET_KEY=not-logged\n",
    ],
    ids=["empty-access", "empty-secret", "both-empty", "empty-secret-with-access", "empty-access-with-secret"],
)
def test_explicit_cleanup_credentials_must_be_complete(tmp_path: Path, extra: str) -> None:
    # GIVEN a static config with explicitly supplied but incomplete cleanup credentials
    config = tmp_path / "s3.conf"
    config.write_text(STATIC_CONFIG + extra)
    # WHEN loading it, THEN it cannot silently fall back to application credentials
    with pytest.raises(ValueError, match="Missing S3 cleanup credential variables") as error:
        provider_from_config(config)
    assert "not-logged" not in str(error.value)
