# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from collections.abc import Collection
from pathlib import Path

from .provider import S3Credentials, S3Provider
from .storage import StaticS3Provider

S3_CONFIG_VARIABLES = (
    "S3_ENDPOINT",
    "S3_REGION",
    "S3_BUCKET_NAMESPACE",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
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
    missing = [name for name in ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY") if not values.get(name)]
    if missing:
        raise ValueError("Missing S3 credential variables: " + ", ".join(missing))
    return StaticS3Provider(
        values["S3_ENDPOINT"],
        values.get("S3_BUCKET_NAMESPACE") or "local",
        S3Credentials(values["S3_ACCESS_KEY"], values["S3_SECRET_KEY"]),
        region=values.get("S3_REGION") or "us-east-1",
    )
