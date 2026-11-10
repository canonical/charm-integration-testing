# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import dataclass
from pathlib import Path

import pytest
from s3_storage import S3Credentials, StaticS3Provider
from s3_storage.configuration import S3_CONFIG_VARIABLES, parse_s3_config, provider_from_config


def test_static_file_constructs_provider_without_http_session(tmp_path: Path) -> None:
    # GIVEN a static configuration file
    path = tmp_path / "s3.conf"
    path.write_text("S3_ENDPOINT=https://s3.test\nS3_ACCESS_KEY=access\nS3_SECRET_KEY=secret\n")
    # WHEN loading it, THEN only a static provider is needed
    provider = provider_from_config(path)
    assert isinstance(provider, StaticS3Provider)
    assert provider.endpoint == "https://s3.test"
    assert provider.region == "us-east-1"
    assert provider.handle.namespace == "local"


def test_file_configuration_preserves_provider_identity(tmp_path: Path) -> None:
    # GIVEN a file with explicit region/namespace and a trailing endpoint slash
    path = tmp_path / "s3.conf"
    path.write_text(
        "S3_ENDPOINT=https://s3.test/\nS3_ACCESS_KEY=access\nS3_SECRET_KEY=secret\n"
        "S3_REGION=local-region\nS3_BUCKET_NAMESPACE=test-environment\n"
    )
    direct = StaticS3Provider(
        "https://s3.test", "test-environment", S3Credentials("access", "secret"), region="local-region"
    )
    # WHEN loading the file, THEN both entrypoints use the same normalized identity
    loaded = provider_from_config(path)
    assert loaded.endpoint == direct.endpoint
    assert loaded.region == direct.region
    assert loaded.handle == direct.handle


def test_parser_defaults_to_generic_keys() -> None:
    # GIVEN a key belonging to an unsupported implementation
    # WHEN the generic parser reads it, THEN the value is not exposed in its error
    with pytest.raises(ValueError, match="Invalid S3 config entry on line 1") as error:
        parse_s3_config("OTHER_AUTH_TOKEN=not-logged\n")
    assert "not-logged" not in str(error.value)


def test_parser_accepts_explicit_backend_allowlist() -> None:
    # GIVEN a backend extending the generic file format
    allowed = (*S3_CONFIG_VARIABLES, "OTHER_AUTH_TOKEN")
    # WHEN its allowlist is supplied, THEN values remain literal
    assert parse_s3_config("OTHER_AUTH_TOKEN=literal=value\n", allowed) == {"OTHER_AUTH_TOKEN": "literal=value"}


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


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("", id="empty-file"),
        pytest.param("# no credentials\n", id="comments-only"),
        pytest.param("S3_REGION=us-east-1\n", id="optional-fields-only"),
        pytest.param("S3_ENDPOINT=\n", id="empty-endpoint"),
        pytest.param("S3_ACCESS_KEY=\n", id="empty-access"),
        pytest.param("S3_SECRET_KEY=\n", id="empty-secret"),
        pytest.param("S3_ENDPOINT=\nS3_ACCESS_KEY=\nS3_SECRET_KEY=\n", id="all-empty"),
        pytest.param("S3_ENDPOINT=https://s3.test\nS3_ACCESS_KEY=access\n", id="missing-secret"),
    ],
)
def test_explicit_provider_entries_require_primary_credentials(tmp_path: Path, content: str) -> None:
    # GIVEN explicitly supplied provider entries without a complete primary identity
    path = tmp_path / "s3.conf"
    path.write_text(content)
    # WHEN loading the file, THEN missing primary fields are reported
    with pytest.raises(ValueError, match="Missing S3 credential variables: S3_"):
        provider_from_config(path)
