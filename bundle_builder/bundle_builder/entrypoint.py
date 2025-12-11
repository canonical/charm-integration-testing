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

from .bundle import Application, ApplicationEndpoint, Bundle, Integration
from .bundle_builder import BundleBuilder
from .charm import Charm
from .charmhub import CharmhubClient, CharmReleaseNotFoundException
from .overrides import OverridesClient


def setup_logging(log_level: str):
    logger = logging.getLogger("bundle_builder")

    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setLevel(getattr(logging, log_level.upper()))

    formatter = logging.Formatter("%(asctime)s - [%(levelname)s] - %(name)s - %(message)s")

    ch.setFormatter(formatter)
    logger.addHandler(ch)

    return logger


def add_args_to_parser(parser: argparse.ArgumentParser):
    parser.add_argument(
        "--charms",
        type=str,
        nargs="+",
        help="Charms to include in the bundle, format <application_name>::<charm>::<channel_or_revision>::<base>. Charm channel or revision, and base may be `default`.",
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
        "--substrate",
        choices=["kubernetes", "openstack"],
        default="kubernetes",
        help="Which substrate is the charm going to be deployed on.",
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
    parser.add_argument(
        "--log-level", type=str.upper, choices=["INFO", "DEBUG", "WARNING", "ERROR", "CRITICAL"], default="INFO"
    )
    parser.add_argument(
        "--charm-priorities-config", type=Path, help="Path to file containing charm priorities", default=None
    )


# Get charms from args
def applications_from_args(
    parser: argparse.ArgumentParser, charmhub_client: CharmhubClient, specs: list[str], arch: str
) -> frozenset[Charm]:
    applications = set()
    for spec in specs:
        # Get charm specs
        try:
            name, charm, channel_or_revision, base = spec.split("::")
        except ValueError:
            parser.error(f"Invalid charm format: '{spec}'")
        channel = None
        revision = None
        if channel_or_revision != "default":
            if channel_or_revision.isnumeric():
                revision = int(channel_or_revision)
            else:
                channel = channel_or_revision
        base = base if base != "default" else None

        # Get charm from store
        try:
            charm = charmhub_client.charm_from_store(
                charm_name=charm,
                charm_channel=channel,
                charm_revision=revision,
                ubuntu_version=base,
                ubuntu_arch=arch,
            )
        except CharmReleaseNotFoundException as e:
            parser.error(f"Charm release not found for '{spec}': {e}")

        # Add application
        applications.add(Application(name=name, charm=charm))
    return frozenset(applications)


# Get integrations from args
def integrations_from_args(parser: argparse.ArgumentParser, specs: list[str]) -> frozenset[Integration]:
    integrations = set()
    for spec in specs:
        # Split specs
        try:
            application_1, application_2 = spec.split("::")
            application_1_name, application_1_endpoint = application_1.split(":")
            application_2_name, application_2_endpoint = application_2.split(":")
        except ValueError:
            parser.error(f"Invalid integration format: '{spec}'")

        # Add integration
        integrations.add(
            Integration(
                {
                    ApplicationEndpoint(application_1_name, application_1_endpoint),
                    ApplicationEndpoint(application_2_name, application_2_endpoint),
                }
            )
        )
    return frozenset(integrations)


# Get platform from args
def platform_from_args(parser: argparse.ArgumentParser, substrate: str) -> str:
    # Lookup substrate to bundle platform
    try:
        return {"kubernetes": "kubernetes", "openstack": "machine"}[substrate]
    except KeyError:
        parser.error(f"Unknown substrate: '{substrate}'")


# Dump to file
def write_to_file(filename: str, content: str, logger: logging.Logger):
    # Get proper file path
    path = Path(filename).absolute().resolve()
    logger.info(f"Writing to '{path}'")

    # Write to file
    path.write_text(content, encoding="utf-8")
    logger.info("Saved file")


def main():
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
    if args.charm_priorities_config is not None and not args.charm_priorities_config.is_file():
        parser.error(f"The charm priorities path '{args.charm_priorities_config}' is not a valid file.")
    overrides_client = OverridesClient(
        charm_metadata_overrides=args.charm_metadata_overrides,
        charm_platform_overrides=args.charm_platform_overrides,
        charm_listing_overrides=args.charm_listing_overrides,
        charm_test_configs=args.charm_test_configs,
        charm_priorities_config=args.charm_priorities_config,
    )

    # Create Charmhub client
    charmhub_client = CharmhubClient(logger=logger, overrides_client=overrides_client)

    # Get base bundle from arguments
    base_bundle = Bundle(
        applications=applications_from_args(parser, charmhub_client, args.charms, args.arch),
        integrations=integrations_from_args(parser, args.integrations),
        platform=platform_from_args(parser, args.substrate),
        arch=args.arch,
    )

    # Validate the base bundle
    try:
        base_bundle.validate()
    except ValueError as e:
        parser.error(f"Invalid bundle: {e}")

    # Build the bundle
    built_bundle = BundleBuilder(charmhub_client=charmhub_client, logger=logger).build(base_bundle)
    logger.info(f"Generated bundle: \n{'-' * 80}\n{built_bundle.export()}{'-' * 80}")

    # Export the bundle to file
    if args.output_file:
        write_to_file(args.output_file, built_bundle.export(), logger)

    # Export the bundle to mermaid diagram
    if args.output_mermaid:
        write_to_file(args.output_mermaid, built_bundle.export_mermaid(), logger)


if __name__ == "__main__":  # pragma: no cover
    main()
