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

from bundle_builder.charm import ENDPOINT_PROVIDES, ENDPOINT_REQUIRES

from .bundle import Application, ApplicationEndpoint, Bundle, Integration
from .bundle_builder import BundleBuilder, UnresolvableBundleError
from .charmhub import CharmhubClient
from .charmhub_http import CharmReleaseNotFoundException
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
        "--charm-scriptlet-overrides",
        type=Path,
        help="Path to folder containing charm scriptlet overrides (.star files)",
        default=None,
    )
    parser.add_argument(
        "--charm-platform-overrides", type=Path, help="Path to folder containing charm platform overrides", default=None
    )
    parser.add_argument(
        "--charm-listing-overrides", type=Path, help="Path to file containing charm listing overrides", default=None
    )
    parser.add_argument("--charm-priorities", type=Path, help="Path to file containing charm priorities", default=None)
    parser.add_argument(
        "--log-level", type=str.upper, choices=["INFO", "DEBUG", "WARNING", "ERROR", "CRITICAL"], default="INFO"
    )


# Get charms from args
def applications_from_args(
    parser: argparse.ArgumentParser, charmhub_client: CharmhubClient, specs: list[str], arch: str
) -> dict[str, Application]:
    applications = {}
    for spec in specs:
        # Get charm specs
        try:
            name, charm_str, channel_str, revision_str, base_str = spec.split("::")
        except ValueError:
            parser.error(
                f"Invalid charm format: '{spec}' - expected format <name>::<charm>::<channel>::<revision>::<base>"
            )

        # Ensure the name is unique
        if name in applications:
            parser.error(f"Duplicate application name in charms: '{name}'")

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

        # Get charm from store
        try:
            charm = charmhub_client.charm_from_store(
                charm_name=charm_str,
                charm_channel=channel,
                charm_revision=revision,
                ubuntu_version=base,
                ubuntu_arch=arch,
            )
        except CharmReleaseNotFoundException as e:
            parser.error(f"Charm release not found for '{spec}': {e}")

        # Add application
        applications[name] = Application(charm=charm)
    return applications


# Get integrations from args
def integrations_from_args(
    parser: argparse.ArgumentParser, specs: list[str], applications: dict[str, Application]
) -> set[Integration]:
    integrations = set()
    for spec in specs:
        # Split specs
        try:
            application_1, application_2 = spec.split("::")
            application_1_name, application_1_endpoint = application_1.split(":")
            application_2_name, application_2_endpoint = application_2.split(":")
        except ValueError:
            parser.error(f"Invalid integration format: '{spec}'")

        # Find requirer and provider applications
        if application_1_name not in applications:
            parser.error(f"Integration refers to unknown application: '{application_1_name}'")
        if application_2_name not in applications:
            parser.error(f"Integration refers to unknown application: '{application_2_name}'")

        # Find endpoints
        charm_endpoint_1 = applications[application_1_name].charm.endpoint.get(application_1_endpoint)
        if charm_endpoint_1 is None:
            parser.error(
                f"Integration refers to unknown endpoint '{application_1_endpoint}' on application '{application_1_name}'"
            )
        charm_endpoint_2 = applications[application_2_name].charm.endpoint.get(application_2_endpoint)
        if charm_endpoint_2 is None:
            parser.error(
                f"Integration refers to unknown endpoint '{application_2_endpoint}' on application '{application_2_name}'"
            )

        # Determine requirer and provider and add integration
        if charm_endpoint_1.type == ENDPOINT_REQUIRES and charm_endpoint_2.type == ENDPOINT_PROVIDES:
            integrations.add(
                Integration(
                    requirer=ApplicationEndpoint(application_1_name, application_1_endpoint),
                    provider=ApplicationEndpoint(application_2_name, application_2_endpoint),
                )
            )
        elif charm_endpoint_1.type == ENDPOINT_PROVIDES and charm_endpoint_2.type == ENDPOINT_REQUIRES:
            integrations.add(
                Integration(
                    requirer=ApplicationEndpoint(application_2_name, application_2_endpoint),
                    provider=ApplicationEndpoint(application_1_name, application_1_endpoint),
                )
            )
        else:
            parser.error(
                f"Invalid integration endpoints: '{application_1_endpoint}' ({charm_endpoint_1.type}) and '{application_2_endpoint}' ({charm_endpoint_2.type})"
            )

    return integrations


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
    if args.charm_scriptlet_overrides is not None and not args.charm_scriptlet_overrides.is_dir():
        parser.error(f"The charm scriptlet overrides path '{args.charm_scriptlet_overrides}' is not a valid directory.")
    if args.charm_platform_overrides is not None and not args.charm_platform_overrides.is_dir():
        parser.error(f"The charm platform overrides path '{args.charm_platform_overrides}' is not a valid directory.")
    if args.charm_listing_overrides is not None and not args.charm_listing_overrides.is_file():
        parser.error(f"The charm listing overrides file '{args.charm_listing_overrides}' is not a valid file.")
    if args.charm_priorities is not None and not args.charm_priorities.is_file():
        parser.error(f"The charm priorities file '{args.charm_priorities}' is not a valid file.")
    overrides_client = OverridesClient(
        charm_scriptlet_overrides=args.charm_scriptlet_overrides,
        charm_platform_overrides=args.charm_platform_overrides,
        charm_listing_overrides=args.charm_listing_overrides,
        charm_priorities=args.charm_priorities,
    )

    # Create Charmhub client
    charmhub_client = CharmhubClient(logger=logger, overrides_client=overrides_client)

    # Get base bundle from arguments
    applications = applications_from_args(parser, charmhub_client, args.charms, args.arch)
    integrations = integrations_from_args(parser, args.integrations, applications)
    base_bundle = Bundle(
        applications=applications,
        integrations=integrations,
        platform=args.platform,
        arch=args.arch,
    )

    # Build the bundle
    bundle_builder = BundleBuilder(charmhub_client=charmhub_client, logger=logger)
    try:
        built_bundle = bundle_builder.build(base_bundle)
    except UnresolvableBundleError as e:
        logger.error(f"Incomplete built bundle: {e.best_bundle.export()}")
        parser.error(f"Unresolvable bundle: {e}")

    logger.info(f"Generated bundle: \n{'-' * 80}\n{built_bundle.export()}{'-' * 80}")

    # Export the bundle to file
    if args.output_file:
        write_to_file(args.output_file, built_bundle.export(), logger)

    # Export the bundle to mermaid diagram
    if args.output_mermaid:
        write_to_file(args.output_mermaid, built_bundle.export_mermaid(), logger)


if __name__ == "__main__":  # pragma: no cover
    main()
