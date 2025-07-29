# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse
import csv

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
    args = parser.parse_args()
    return args


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
    with open(args.test_executions, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["Artefact.family"] != "charm":
                continue

            total += 1
            if row["TestExecution.status"] == "PASSED":
                passed += 1
            elif row["TestExecution.status"] == "FAILED":
                failed += 1
            elif row["TestExecution.status"] == "ENDED_PREMATURELY":
                ended_early += 1
            else:
                other += 1

    # Log results
    logger.info(f"Passed: {total}")
    logger.info(f"Passed: {passed}")
    logger.info(f"Failed: {failed}")
    logger.info(f"Ended early: {ended_early}")
    logger.info(f"Other: {other}")
    logger.info(f"Pass rate : {passed / (passed + failed)}")


if __name__ == "__main__":
    main()
