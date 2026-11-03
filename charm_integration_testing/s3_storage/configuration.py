# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from collections.abc import Collection
from pathlib import Path

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
)


def parse_s3_config(content: str, allowed_variables: Collection[str] = S3_CONFIG_VARIABLES) -> dict[str, str]:
    """Read allowlisted, single-line KEY=value entries without evaluating shell syntax."""
    values: dict[str, str] = {}
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name not in allowed_variables:
            raise ValueError(f"Invalid S3 config entry on line {line_number}")
        if name in values:
            raise ValueError(f"Duplicate S3 config entry for {name}")
        values[name] = value
    return values


def provider_from_config(path: Path) -> S3Provider:
    """Construct a static provider from a file, independent of the process environment."""
    values = parse_s3_config(path.expanduser().read_text(encoding="utf-8"))
    provider = provider_from_environment(values)
    if provider is None:
        raise ValueError(f"No S3 provider configured in '{path}'")
    return provider
