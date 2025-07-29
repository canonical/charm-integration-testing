# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse
import json

from charmhub_mapper.charmhub_mapper import CharmhubMap
from charmhub_mapper.logger import get_logger


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--debug",
        help="Enable debug logging",
        action="store_true",
    )
    parser.add_argument(
        "--map-file",
        help="Charmhub map to load",
        required=True,
    )
    return parser.parse_args()


def main():
    # Parse args
    args = get_args()

    # Get logger
    logger = get_logger("top_level_interfaces", args.debug)

    # Load charmhub map
    logger.info("Loading Charmhub map")
    with open(args.map_file, "r") as file:
        charmhub_map = CharmhubMap(**json.load(file))

    # Count top level integrations by interfaces
    top_level_interfaces: dict[str, int] = {}
    for charm in charmhub_map.charms:
        for platform in charm.platforms:
            for arch in platform.arches:
                for version in arch.versions:
                    for integration in version.minimal_bundle.integrations:
                        for application_endpoint in integration:
                            if application_endpoint.application != "target":
                                continue

                            interface = version.minimal_bundle.application_endpoints[application_endpoint].interface
                            if interface in top_level_interfaces:
                                top_level_interfaces[interface] += 1
                            else:
                                top_level_interfaces[interface] = 1

    # Print interface counts
    for interface, count in reversed(sorted(top_level_interfaces.items(), key=lambda x: x[1])):
        logger.info(f"{interface}: {count}")


if __name__ == "__main__":
    main()
