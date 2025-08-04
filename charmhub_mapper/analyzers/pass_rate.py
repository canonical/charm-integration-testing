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
        type=Path,
        required=False,
    )
    parser.add_argument(
        "--exclude-containing-charms",
        nargs="*",
        type=str,
        default=[],
        help="Exclude test executions that deployed these charms from the statistics",
    )
    args = parser.parse_args()
    if len(args.exclude_containing_charms) > 0 and not args.generated_bundles:
        parser.error(f"Please supply path to directory containing generated bundles.")
    return args


def get_bundle_charms(test_execution_id: int, generated_bundles: Path) -> set[str]:
    bundle_path = generated_bundles / f"{test_execution_id}.yaml"
    if not bundle_path.exists():
        return set()

    with bundle_path.open("r") as f:
        bundle = yaml.safe_load(f)
        return {application["charm"] for application in bundle.get("applications", {}).values()}


def main():
    # Parse args
    args = get_args()

    # Get logger
    logger = get_logger("bundles_that_passed", args.debug)

    # Calculate pass rate
    total = 0
    passed = 0
    failed = 0
    ended_early = 0
    other = 0
    excluded = 0
    with open(args.test_executions, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["Artefact.family"] != "charm":
                continue

            total += 1

            if len(args.exclude_containing_charms) > 0:
                deployed_charms = get_bundle_charms(row["TestExecution.id"], args.generated_bundles)
                if set(args.exclude_containing_charms) & deployed_charms:
                    excluded += 1
                    continue

            if row["TestExecution.status"] == "PASSED":
                passed += 1
            elif row["TestExecution.status"] == "FAILED":
                failed += 1
            elif row["TestExecution.status"] == "ENDED_PREMATURELY":
                ended_early += 1
            else:
                other += 1

    # Log results
    logger.info(f"Total: {total}")
    logger.info(f"Passed: {passed}")
    logger.info(f"Failed: {failed}")
    logger.info(f"Ended early: {ended_early}")
    logger.info(f"Other: {other}")
    logger.info(f"Excluded: {excluded}")
    logger.info(f"Pass rate : {passed / (passed + failed)}")


if __name__ == "__main__":
    main()
