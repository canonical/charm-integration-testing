#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse
from datetime import timedelta
from pathlib import Path

import requests
from s3_storage_swift.configuration import provider_from_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete stale harness S3 buckets owned by an S3 configuration")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--age-minutes", type=float, default=240)
    args = parser.parse_args()
    with requests.Session() as session:
        provider = provider_from_config(args.config, session)
        for bucket in provider.cleanup_stale(timedelta(minutes=args.age_minutes)):
            print(f"Removed S3 bucket {bucket}")


if __name__ == "__main__":
    main()
