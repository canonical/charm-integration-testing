#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse
import os
import sys
from pathlib import Path

SWIFT_CONFIG_REQUIRED_VARIABLES = (
    "S3_OS_AUTH_URL",
    "S3_OS_USERNAME",
    "S3_OS_PASSWORD",
    "S3_OS_USER_DOMAIN_NAME",
    "S3_OS_PROJECT_NAME",
    "S3_OS_PROJECT_DOMAIN_NAME",
    "S3_OS_STORAGE_URL",
    "S3_CLEANUP_ACCESS_KEY",
    "S3_CLEANUP_SECRET_KEY",
)
SWIFT_CONFIG_OPTIONAL_VARIABLES = ("S3_ENDPOINT",)
SWIFT_CONFIG_VARIABLES = (*SWIFT_CONFIG_REQUIRED_VARIABLES, *SWIFT_CONFIG_OPTIONAL_VARIABLES)


def parse_swift_config(config: str) -> dict[str, str]:
    """Parse the supported single-line KEY=value format without evaluating shell syntax."""
    values: dict[str, str] = {}
    for line_number, line in enumerate(config.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name not in SWIFT_CONFIG_VARIABLES:
            raise ValueError(f"Invalid SWIFT_CONFIG entry on line {line_number}")
        if name in values:
            raise ValueError(f"Duplicate SWIFT_CONFIG entry for {name}")
        values[name] = value
    return values


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load an environment's .env-style Swift credentials into GitHub Actions."
    )
    parser.add_argument("secret_name", help="GitHub Actions secret name used as the storage namespace")
    parser.add_argument("--prefix", default="", help="prefix added to the exported environment variable names")
    parser.add_argument(
        "--optional",
        action="store_true",
        help="report unavailable when the config is absent instead of requiring all values",
    )
    args = parser.parse_args()
    config = os.environ.get("SWIFT_CONFIG", "")
    try:
        values = parse_swift_config(config)
    except ValueError as exc:
        print(f"::error::{exc}")
        return 1

    if not values and args.optional:
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write("available=false\n")
        return 0

    missing = [name for name in SWIFT_CONFIG_REQUIRED_VARIABLES if not values.get(name)]
    if missing:
        print(f"::error::{args.secret_name} is missing: {', '.join(missing)}")
        return 1

    for value in values.values():
        escaped_value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::add-mask::{escaped_value}")
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as environment:
        for name in SWIFT_CONFIG_VARIABLES:
            environment.write(f"{args.prefix}{name}={values.get(name, '')}\n")
        environment.write(f"{args.prefix}S3_BUCKET_NAMESPACE={args.secret_name}\n")
    if args.optional:
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write("available=true\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
