# Copyright (C) 2025 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import argparse
import logging
from pathlib import Path

from .bundle import ApplicationEndpoint, Integration
from .bundle_builder import ApplicationConstraint, BundleBuilder, UnresolvableBundleError
from .charmhub import CharmhubClient
from .overrides import OverridesClient


def setup_logging(log_level: str) -> logging.Logger:
    logger = logging.getLogger("bundle_builder")

    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setLevel(getattr(logging, log_level.upper()))

    formatter = logging.Formatter("%(asctime)s - [%(levelname)s] - %(name)s - %(message)s")

    ch.setFormatter(formatter)
    logger.addHandler(ch)

    return logger


def add_args_to_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--charms",
        type=str,
        nargs="+",
        help="Charms to include in the bundle, format <application_name>::<charm>::<channel>::<revision>::<base>. "
        "Channel format is '<track>/<risk>/<branch>' (track and branch optional), revision is an integer. "
        "Use 'default' for channel, revision, or base to use defaults.",
        required=True,
    )
    parser.add_argument(
        "--integrations",
        type=str,
        nargs="+",
        help="Integrations to include in the bundle, format <application_name>:<endpoint>::<application_name>:<endpoint>.",
        default=[],
    )
    parser.add_argument(
        "--arch",
        type=str,
        help="Architecture to use for the bundle",
        choices=["amd64"],
        default="amd64",
    )
    parser.add_argument(
        "--platform",
        choices=["kubernetes", "machine"],
        default="kubernetes",
        help="Which platform the bundle is going to be deployed on.",
    )
    parser.add_argument("--output-file", type=str, help="Where to save the generated bundle.")
    parser.add_argument("--output-mermaid", type=str, help="Where to save the generated mermaid diagram.")
    parser.add_argument(
        "--charm-metadata-overrides", type=Path, help="Path to folder containing charm metadata overrides", default=None
    )
    parser.add_argument(
        "--charm-platform-overrides", type=Path, help="Path to folder containing charm platform overrides", default=None
    )
    parser.add_argument(
        "--charm-listing-overrides", type=Path, help="Path to file containing charm listing overrides", default=None
    )
    parser.add_argument("--charm-test-configs", type=Path, help="Path to folder containing charm configs", default=None)
    parser.add_argument("--charm-priorities", type=Path, help="Path to file containing charm priorities", default=None)
    parser.add_argument(
        "--log-level", type=str.upper, choices=["INFO", "DEBUG", "WARNING", "ERROR", "CRITICAL"], default="INFO"
    )


# Get charms from args
def applications_from_args(parser: argparse.ArgumentParser, specs: list[str]) -> dict[str, ApplicationConstraint]:
    constraints = {}
    for spec in specs:
        # Get charm specs
        try:
            name, charm_str, channel_str, revision_str, base_str = spec.split("::")
        except ValueError:
            parser.error(
                f"Invalid charm format: '{spec}' - expected format <name>::<charm>::<channel>::<revision>::<base>"
            )

        # Parse channel
        channel = None if channel_str == "default" else channel_str

        # Parse revision
        revision = None
        if revision_str != "default":
            if not revision_str.isnumeric():
                parser.error(f"Invalid revision in '{spec}': revision must be numeric, got '{revision_str}'")
            revision = int(revision_str)

        # Parse base
        base = None if base_str == "default" else base_str

        # Add application constraint
        constraints[name] = ApplicationConstraint(
            name=name, charm=charm_str, channel=channel, revision=revision, base=base
        )
    return constraints


# Get integrations from args
def integrations_from_args(parser: argparse.ArgumentParser, specs: list[str]) -> set[Integration]:
    constraints = set()
    for spec in specs:
        # Split specs
        try:
            application_1, application_2 = spec.split("::")
            application_1_name, application_1_endpoint = application_1.split(":")
            application_2_name, application_2_endpoint = application_2.split(":")
        except ValueError:
            parser.error(f"Invalid integration format: '{spec}'")

        # Add constraints
        constraints.add(
            Integration(
                ApplicationEndpoint(application=application_1_name, endpoint=application_1_endpoint),
                ApplicationEndpoint(application=application_2_name, endpoint=application_2_endpoint),
            )
        )
    return constraints


# Dump the bundle to file
def write_to_file(filename: str, content: str, logger: logging.Logger) -> None:
    # Get proper file path
    path = Path(filename).absolute().resolve()
    logger.info(f"Writing to '{path}'")

    # Write to file
    path.write_text(content, encoding="utf-8")
    logger.info("Saved file")


def main() -> None:
    # Get CLI arguments
    parser = argparse.ArgumentParser()
    add_args_to_parser(parser)
    args = parser.parse_args()

    # Get logger
    logger = setup_logging(args.log_level)

    # Create override client
    if args.charm_metadata_overrides is not None and not args.charm_metadata_overrides.is_dir():
        parser.error(f"The charm metadata overrides path '{args.charm_metadata_overrides}' is not a valid directory.")
    if args.charm_platform_overrides is not None and not args.charm_platform_overrides.is_dir():
        parser.error(f"The charm platform overrides path '{args.charm_platform_overrides}' is not a valid directory.")
    if args.charm_listing_overrides is not None and not args.charm_listing_overrides.is_file():
        parser.error(f"The charm listing overrides file '{args.charm_listing_overrides}' is not a valid file.")
    if args.charm_test_configs is not None and not args.charm_test_configs.is_dir():
        parser.error(f"The charm test configs path '{args.charm_test_configs}' is not a valid directory.")
    if args.charm_priorities is not None and not args.charm_priorities.is_file():
        parser.error(f"The charm priorities file '{args.charm_priorities}' is not a valid file.")
    overrides_client = OverridesClient(
        charm_metadata_overrides=args.charm_metadata_overrides,
        charm_platform_overrides=args.charm_platform_overrides,
        charm_listing_overrides=args.charm_listing_overrides,
        charm_test_configs=args.charm_test_configs,
        charm_priorities=args.charm_priorities,
    )

    # Create Charmhub client
    charmhub_client = CharmhubClient(logger=logger, overrides_client=overrides_client)

    # Build the bundle
    bundle_builder = BundleBuilder(charmhub_client=charmhub_client, logger=logger)
    try:
        bundle = bundle_builder.build(
            applications=applications_from_args(parser, args.charms),
            integrations=integrations_from_args(parser, args.integrations),
            platform=args.platform,
            arch=args.arch,
        )
    except UnresolvableBundleError as e:
        parser.error(f"Unresolvable bundle: {e}")

    logger.info(f"Generated bundle: \n{'-' * 80}\n{bundle.export()}{'-' * 80}")

    # Export the bundle to file
    if args.output_file:
        write_to_file(args.output_file, bundle.export(), logger)

    # Export the bundle to mermaid diagram
    if args.output_mermaid:
        write_to_file(args.output_mermaid, bundle.export_mermaid(), logger)


if __name__ == "__main__":  # pragma: no cover
    main()
