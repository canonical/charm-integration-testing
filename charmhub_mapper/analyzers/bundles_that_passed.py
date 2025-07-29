# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse
import csv
from pathlib import Path

import yaml

from charmhub_mapper.logger import get_logger


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--debug",
        help="Enable debug logging",
        action="store_true",
    )
    parser.add_argument(
        "--test-executions",
        help="Test observer test executions CSV",
        required=True,
    )
    parser.add_argument(
        "--generated-bundles",
        help="Directory containing generated bundles for test executions",
        required=True,
    )
    parser.add_argument(
        "--bundle-size",
        help="Minimum size of bundles to print output for",
        type=int,
        default=2,
    )
    args = parser.parse_args()
    return args


def main():
    # Parse args
    args = get_args()

    # Get logger
    logger = get_logger("bundles_that_passed", args.debug)

    # Calculate edge weights and colors using test executions
    with open(args.test_executions, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["Artefact.family"] != "charm":
                continue
            if row["TestExecution.status"] != "PASSED":
                continue

            # Get integrations from this test execution
            integrations = set()
            test_plan_attributes = row["TestExecution.test_plan"].split("/")
            integrations.add(frozenset({test_plan_attributes[1].split(":")[0], test_plan_attributes[3].split(":")[0]}))

            # Get integrations from bundle
            bundle_path = Path(args.generated_bundles) / f"{row['TestExecution.id']}.yaml"
            if bundle_path.exists():
                with bundle_path.open("r") as f:
                    bundle = yaml.safe_load(f)
                    artefact = f"{row['Artefact.name']}:{row['Artefact.version']}"
                    test_plan = f"{row['TestExecution.test_plan']}"
                    charms = sorted({app["charm"] for app in bundle["applications"].values()})
                    if len(charms) > args.bundle_size:
                        logger.info(f"{artefact}: {test_plan}, which has {charms}")


if __name__ == "__main__":
    main()
