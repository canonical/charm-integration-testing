# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

import requests

from .environment import provider_from_environment
from .provider import S3Provider

S3_CONFIG_VARIABLES = (
    "S3_ENDPOINT",
    "S3_REGION",
    "S3_BUCKET_NAMESPACE",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "S3_CLEANUP_ACCESS_KEY",
    "S3_CLEANUP_SECRET_KEY",
    "S3_OS_AUTH_URL",
    "S3_OS_USERNAME",
    "S3_OS_PASSWORD",
    "S3_OS_USER_DOMAIN_NAME",
    "S3_OS_PROJECT_NAME",
    "S3_OS_PROJECT_DOMAIN_NAME",
    "S3_OS_STORAGE_URL",
)


def parse_s3_config(content: str) -> dict[str, str]:
    """Read unquoted, single-line KEY=value entries without evaluating shell syntax."""
    values: dict[str, str] = {}
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name not in S3_CONFIG_VARIABLES:
            raise ValueError(f"Invalid S3 config entry on line {line_number}")
        if name in values:
            raise ValueError(f"Duplicate S3 config entry for {name}")
        values[name] = value
    return values


def provider_from_config(path: Path, session: requests.Session) -> S3Provider:
    """Construct a provider from a file, independent of the process environment."""
    values = parse_s3_config(path.expanduser().read_text(encoding="utf-8"))
    if any(name.startswith("S3_OS_") for name in values):
        if "S3_ACCESS_KEY" in values or "S3_SECRET_KEY" in values:
            raise ValueError("Cannot mix static and Swift credentials in an S3 configuration file")
        if not values.get("S3_OS_STORAGE_URL"):
            raise ValueError("Missing Swift credential variable: S3_OS_STORAGE_URL")
    cleanup_variables = ("S3_CLEANUP_ACCESS_KEY", "S3_CLEANUP_SECRET_KEY")
    if any(name in values for name in cleanup_variables):
        missing = [name for name in cleanup_variables if not values.get(name)]
        if missing:
            raise ValueError("Missing S3 cleanup credential variables: " + ", ".join(missing))
    provider = provider_from_environment(values, session)
    if provider is None:
        raise ValueError(f"No S3 provider configured in '{path}'")
    return provider
