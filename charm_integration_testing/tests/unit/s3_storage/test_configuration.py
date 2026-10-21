# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

import pytest
from s3_storage import StaticS3Provider
from s3_storage.configuration import S3_CONFIG_VARIABLES, parse_s3_config, provider_from_config


def test_static_file_constructs_provider_without_http_session(tmp_path: Path) -> None:
    # GIVEN a static configuration file
    path = tmp_path / "s3.conf"
    path.write_text("S3_ENDPOINT=https://s3.test\nS3_ACCESS_KEY=access\nS3_SECRET_KEY=secret\n")
    # WHEN loading it, THEN only a static provider is needed
    assert isinstance(provider_from_config(path), StaticS3Provider)


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


def test_explicit_empty_cleanup_credentials_are_not_ignored(tmp_path: Path) -> None:
    # GIVEN static credentials and an explicitly empty management identity
    path = tmp_path / "s3.conf"
    path.write_text(
        "S3_ENDPOINT=https://s3.test\nS3_ACCESS_KEY=access\nS3_SECRET_KEY=secret\n"
        "S3_CLEANUP_ACCESS_KEY=\nS3_CLEANUP_SECRET_KEY=\n"
    )
    # WHEN loading the file, THEN management credentials cannot silently fall back
    with pytest.raises(ValueError, match="Missing S3 cleanup credential variables"):
        provider_from_config(path)
