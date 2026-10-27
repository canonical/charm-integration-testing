#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse
import math
from datetime import timedelta
from pathlib import Path

from s3_storage.configuration import provider_from_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete stale harness S3 buckets owned by an S3 configuration")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--age-minutes", type=float, default=240)
    args = parser.parse_args()
    if not math.isfinite(args.age_minutes) or args.age_minutes <= 0:
        parser.error("--age-minutes must be a positive finite number")
    try:
        age = timedelta(minutes=args.age_minutes)
    except OverflowError:
        parser.error("--age-minutes is outside the supported duration range")
    if age <= timedelta(0):
        parser.error("--age-minutes must represent at least one microsecond")
    provider = provider_from_config(args.config)
    for bucket in provider.cleanup_stale(age):
        print(f"Removed S3 bucket {bucket}")


if __name__ == "__main__":
    main()
