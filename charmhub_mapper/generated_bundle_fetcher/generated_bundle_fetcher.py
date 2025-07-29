# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse
import csv
from pathlib import Path

import requests

from charmhub_mapper.logger import get_logger


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--debug",
        help="Enable debug logging",
        action="store_true",
    )
    parser.add_argument(
        "--swift-url",
        type=str,
        help="Swift url base with auth key",
        required=True,
    )
    parser.add_argument(
        "--test-observer-test-executions",
        type=str,
        help="CSV file from test observer",
        required=True,
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Location to write output",
        default="generated_bundles_by_test_execution",
    )
    return parser.parse_args()


def main():
    # Parse args
    args = get_args()

    # Get logger
    logger = get_logger("test_observer_mapper", args.debug)

    # Make output directory
    output_path = Path(args.output)
    output_path.mkdir(parents=True, exist_ok=True)

    # Read test observer test executions
    with open(args.test_observer_test_executions, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["Artefact.family"] != "charm":
                continue
            if row["TestExecution.status"] not in {"FAILED", "PASSED"}:
                continue
            logger.debug(f"Checking {row['Artefact.name']}:{row['Artefact.version']}: {row['TestExecution.test_plan']}")

            # Download generated bundle
            response = requests.get(f"{args.swift_url}/{row['TestExecution.id']}/generated-bundle.yaml")
            if response.status_code == 404:
                continue
            response.raise_for_status()

            # Write out
            with (output_path / f"{row['TestExecution.id']}.yaml").open("w", encoding="utf-8") as f:
                f.write(response.text)


if __name__ == "__main__":
    main()
