# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from collections.abc import Mapping
from pathlib import Path

import requests
from s3_storage import S3Credentials, S3Provider
from s3_storage.configuration import S3_CONFIG_VARIABLES, parse_s3_config
from s3_storage.environment import provider_from_environment as static_provider_from_environment
from s3_storage.identity import provider_identity

from .credentials import KeystoneClient, SwiftCredentials
from .provider import SwiftS3Provider

SWIFT_CONFIG_VARIABLES = (
    "S3_OS_AUTH_URL",
    "S3_OS_USERNAME",
    "S3_OS_PASSWORD",
    "S3_OS_USER_DOMAIN_NAME",
    "S3_OS_PROJECT_NAME",
    "S3_OS_PROJECT_DOMAIN_NAME",
    "S3_OS_STORAGE_URL",
)
_CLEANUP_VARIABLES = ("S3_CLEANUP_ACCESS_KEY", "S3_CLEANUP_SECRET_KEY")


def provider_from_environment(
    environ: Mapping[str, str], session: requests.Session, prefix: str = ""
) -> S3Provider | None:
    """Compose static and Swift providers from explicit, optionally prefixed values."""
    values = {
        name: environ[f"{prefix}{name}"]
        for name in (*S3_CONFIG_VARIABLES, *SWIFT_CONFIG_VARIABLES)
        if f"{prefix}{name}" in environ
    }
    if not any(name in values for name in SWIFT_CONFIG_VARIABLES):
        return static_provider_from_environment(environ, prefix)
    if "S3_ACCESS_KEY" in values or "S3_SECRET_KEY" in values:
        raise ValueError("Cannot mix static and Swift credentials in an S3 configuration file")
    if not values.get("S3_OS_STORAGE_URL"):
        raise ValueError(f"Missing Swift credential variable: {prefix}S3_OS_STORAGE_URL")
    missing = [f"{prefix}{name}" for name in SWIFT_CONFIG_VARIABLES if not values.get(name)]
    if missing:
        raise ValueError("Missing Swift credential variables: " + ", ".join(missing))
    missing_cleanup = [f"{prefix}{name}" for name in _CLEANUP_VARIABLES if not values.get(name)]
    if missing_cleanup:
        raise ValueError("Missing S3 cleanup credential variables: " + ", ".join(missing_cleanup))
    credentials = SwiftCredentials(
        values["S3_OS_AUTH_URL"],
        values["S3_OS_USERNAME"],
        values["S3_OS_PASSWORD"],
        values["S3_OS_USER_DOMAIN_NAME"],
        values["S3_OS_PROJECT_NAME"],
        values["S3_OS_PROJECT_DOMAIN_NAME"],
        values["S3_OS_STORAGE_URL"],
    )
    endpoint = (values.get("S3_ENDPOINT") or credentials.s3_endpoint()).rstrip("/")
    namespace = values.get("S3_BUCKET_NAMESPACE") or "local"
    region = values.get("S3_REGION") or "us-east-1"
    return SwiftS3Provider(
        endpoint,
        namespace,
        KeystoneClient(credentials, session),
        S3Credentials(values["S3_CLEANUP_ACCESS_KEY"], values["S3_CLEANUP_SECRET_KEY"]),
        region=region,
        identity=provider_identity(
            "swift",
            endpoint,
            namespace,
            region,
            credentials.auth_endpoint(),
            credentials.username,
            credentials.password,
            credentials.user_domain_name,
            credentials.project_name,
            credentials.project_domain_name,
            credentials.storage_url,
            values["S3_CLEANUP_ACCESS_KEY"],
            values["S3_CLEANUP_SECRET_KEY"],
        ),
    )


def provider_from_config(path: Path, session: requests.Session) -> S3Provider:
    """Load either supported provider using the shared literal file format."""
    values = parse_s3_config(
        path.expanduser().read_text(encoding="utf-8"), (*S3_CONFIG_VARIABLES, *SWIFT_CONFIG_VARIABLES)
    )
    provider = provider_from_environment(values, session)
    if provider is None:
        raise ValueError(f"No S3 provider configured in '{path}'")
    return provider
