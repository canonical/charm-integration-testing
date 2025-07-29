# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse
import csv
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from urllib.parse import urljoin

import requests

from charmhub_mapper.logger import get_logger


def parse_date(date_string: str):
    try:
        return datetime.strptime(date_string, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"Date '{date_string}' is not in YYYY-MM-DD format.")


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--debug",
        help="Enable debug logging",
        action="store_true",
    )
    parser.add_argument(
        "--test-observer-api",
        type=str,
        help="Test Observer API URL",
        default="https://test-observer-api.canonical.com/",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Location to write output",
        default="test_observer_map",
    )
    parser.add_argument(
        "--start",
        type=parse_date,
        help="Start date (inclusive) in YYYY-MM-DD format. Defaults to 2 weeks ago.",
        default=datetime.now() - timedelta(weeks=2),
    )
    parser.add_argument(
        "--end",
        type=parse_date,
        help="End date (inclusive) in YYYY-MM-DD format. Defaults to today.",
        default=datetime.now(),
    )
    return parser.parse_args()


def fetch_test_observer_csv(output_path, api, logger, start, end):
    with output_path.open("w", encoding="utf-8") as f:
        logger.info(f"Fetching csv from {api}")
        writer = csv.writer(f)
        write_header = True
        start = datetime(year=start.year, month=start.month, day=start.day, tzinfo=timezone.utc)
        end = datetime(year=end.year, month=end.month, day=end.day, tzinfo=timezone.utc)
        step = timedelta(days=1)
        while start < end:
            logger.debug(f"Fetching for day {start.strftime('%Y-%m-%d')}")

            response = requests.get(
                api,
                params={
                    "start_date": start.isoformat(),
                    "end_date": (start + step).isoformat(),
                },
            )
            response.raise_for_status()

            csv_content = StringIO(response.text)
            reader = csv.reader(csv_content)

            for i, row in enumerate(reader):
                if i == 0 and not write_header:
                    continue
                writer.writerow(row)
            write_header = False

            start += step

        logger.info(f"Retrieved csv from {api}")


def main():
    # Parse args
    args = get_args()

    # Get logger
    logger = get_logger("test_observer_mapper", args.debug)

    # Make output directory
    output_path = Path(args.output)
    output_path.mkdir(parents=True, exist_ok=True)

    # Retrieve test observer test report
    fetch_test_observer_csv(
        output_path / "test_results.csv",
        urljoin(args.test_observer_api, "/v1/reports/test-results"),
        logger,
        args.start,
        args.end,
    )
    fetch_test_observer_csv(
        output_path / "test_executions.csv",
        urljoin(args.test_observer_api, "/v1/reports/test-executions"),
        logger,
        args.start,
        args.end,
    )


if __name__ == "__main__":
    main()
