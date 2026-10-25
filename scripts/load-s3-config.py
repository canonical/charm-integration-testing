#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse
import os
import re
import sys
from pathlib import Path

S3_STORAGE_CONFIG_REQUIRED_VARIABLES = (
    "S3_ENDPOINT",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "S3_BUCKET_NAMESPACE",
)
S3_STORAGE_CONFIG_OPTIONAL_VARIABLES = ("S3_REGION", "S3_CLEANUP_ACCESS_KEY", "S3_CLEANUP_SECRET_KEY")
S3_STORAGE_CONFIG_VARIABLES = (*S3_STORAGE_CONFIG_REQUIRED_VARIABLES, *S3_STORAGE_CONFIG_OPTIONAL_VARIABLES)


def parse_s3_storage_config(config: str) -> dict[str, str]:
    """Parse the supported single-line KEY=value format without evaluating shell syntax."""
    values: dict[str, str] = {}
    for line_number, line in enumerate(config.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name not in S3_STORAGE_CONFIG_VARIABLES:
            raise ValueError(f"Invalid S3_STORAGE_CONFIG entry on line {line_number}")
        if name in values:
            raise ValueError(f"Duplicate S3_STORAGE_CONFIG entry for {name}")
        values[name] = value
    return values


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write an environment's shared S3 credentials to a protected S3 configuration file."
    )
    parser.add_argument("secret_name", help="GitHub Actions secret name used in diagnostics")
    parser.add_argument("--output-file", type=Path, required=True, help="new mode-600 configuration file")
    parser.add_argument(
        "--optional",
        action="store_true",
        help="report unavailable when the config is absent instead of requiring all values",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_]+", args.secret_name):
        parser.error("secret_name must contain only letters, digits, and underscores")
    config = os.environ.get("S3_STORAGE_CONFIG", "")
    try:
        values = parse_s3_storage_config(config)
    except ValueError as exc:
        print(f"::error::{exc}")
        return 1

    if not values and args.optional:
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write("available=false\n")
        return 0

    missing = [name for name in S3_STORAGE_CONFIG_REQUIRED_VARIABLES if not values.get(name, "").strip()]
    if missing:
        print(f"::error::{args.secret_name} is missing: {', '.join(missing)}")
        return 1
    cleanup_names = ("S3_CLEANUP_ACCESS_KEY", "S3_CLEANUP_SECRET_KEY")
    if sum(bool(values.get(name, "").strip()) for name in cleanup_names) == 1:
        print(f"::error::{args.secret_name} must set both or neither of: {', '.join(cleanup_names)}")
        return 1

    for value in values.values():
        escaped_value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::add-mask::{escaped_value}")
    descriptor = os.open(args.output_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
        for name, value in values.items():
            config_file.write(f"{name}={value}\n")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"config_path={args.output_file.resolve()}\navailable=true\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
