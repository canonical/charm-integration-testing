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
from .charmhub import CharmhubClient
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
        choices=["kubernetes"],
        default="kubernetes",
        help="Which substrate is the charm going to be deployed on. Only kubernetes is enabled for now.",
    )
    parser.add_argument("--output-file", type=str, help="Where to save the generated bundle.")
    parser.add_argument(
        "--charm-metadata-overrides", type=Path, help="Path to folder containing charm metadata overrides", default=None
    )
    parser.add_argument(
        "--log-level", type=str.upper, choices=["INFO", "DEBUG", "WARNING", "ERROR", "CRITICAL"], default="INFO"
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
        applications.add(
            Application(
                name=name,
                charm=charmhub_client.charm_from_store(
                    charm_name=charm,
                    charm_channel=channel,
                    charm_revision=revision,
                    ubuntu_version=base,
                    ubuntu_arch=arch,
                ),
            )
        )
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
        return {"kubernetes": "kubernetes"}[substrate]
    except KeyError:
        parser.error(f"Unknown substrate: '{substrate}'")


# Dump the bundle to file
def export_bundle_to_file(filename: str, bundle: Bundle, logger: logging.Logger):
    # Get proper file path
    path = Path(filename).absolute().resolve()
    logger.info(f"Saving bundle to '{path}'")

    # Write to file
    path.write_text(bundle.export(), encoding="utf-8")
    logger.info("Saved bundle")


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
    overrides_client = OverridesClient(args.charm_metadata_overrides)

    # Create Charmhub client
    charmhub_client = CharmhubClient(logger=logger, overrides_client=overrides_client)

    # Get base bundle from arguments
    base_bundle = Bundle(
        applications=applications_from_args(parser, charmhub_client, args.charms, args.arch),
        integrations=integrations_from_args(parser, args.integrations),
        platform=platform_from_args(parser, args.substrate),
        arch=args.arch,
    )

    # Build the bundle
    built_bundle = BundleBuilder(charmhub_client=charmhub_client, logger=logger).build(base_bundle)
    logger.info(f"Generated bundle: \n{'-' * 80}\n{built_bundle.export()}{'-' * 80}")

    # Export the bundle to file
    if args.output_file:
        export_bundle_to_file(args.output_file, built_bundle, logger)


if __name__ == "__main__":  # pragma: no cover
    main()
