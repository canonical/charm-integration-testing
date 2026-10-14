#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse
import os
from datetime import timedelta

import requests
from s3_storage.environment import provider_from_environment


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete stale harness S3 buckets in S3_BUCKET_NAMESPACE")
    parser.add_argument("--age-minutes", type=float, default=240)
    args = parser.parse_args()
    with requests.Session() as session:
        provider = provider_from_environment(os.environ, session)
        if provider is None:
            parser.error("Swift or S3 credentials are required")
        for bucket in provider.cleanup_stale(timedelta(minutes=args.age_minutes)):
            print(f"Removed S3 bucket {bucket}")


if __name__ == "__main__":
    main()
