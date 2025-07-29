# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse

from pydantic import RootModel

from bundle_builder import BundleBuilder, CharmhubClient

from .charmhub_mapper import CharmhubMap, CharmhubMapper
from .logger import get_logger


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--debug",
        help="Enable debug logging",
        action="store_true",
    )
    parser.add_argument(
        "--arch",
        type=str,
        help="Architecture to use for the bundles",
        choices=["amd64"],
        default="amd64",
    )
    parser.add_argument(
        "--platform",
        type=str,
        help="Platform to use for the bundles",
        choices=["kubernetes"],
        default="kubernetes",
    )
    parser.add_argument(
        "--charms",
        type=str,
        nargs="+",
        help="Charms to check. Omit to check all charms",
        default=None,
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Location to write output",
        default="charmhub_map.json",
    )
    parser.add_argument(
        "--map-only-base-bundle",
        help="Map only minimal bundle for a charm, not bundles of all integrations",
        action="store_true",
    )
    parser.add_argument(
        "--store-node-tree",
        help="Whether to store the node tree for the bundle resolution",
        action="store_true",
    )
    return parser.parse_args()


def main():
    # Parse args
    args = get_args()

    # Get logger
    logger = get_logger("charmhub_mapper", args.debug)

    # Create Charmhub client
    charmhub_client = CharmhubClient(logger=logger.getChild("charmhub_client"))

    # Create bundle builder
    bundle_builder = BundleBuilder(
        charmhub_client=charmhub_client, logger=logger.getChild("bundle_builder"), max_nodes_visited=10000
    )

    # Create scenario mapper
    charmhub_mapper = CharmhubMapper(
        charmhub_client=charmhub_client,
        bundle_builder=bundle_builder,
        logger=logger.getChild("charmhub_mapper"),
        map_only_base_bundle=args.map_only_base_bundle,
        with_node_tree=args.store_node_tree,
    )

    # Build charmhub map
    logger.info("Mapping Charmhub")
    charmhub_map = charmhub_mapper.map_charmhub(charms=args.charms, platforms={args.platform}, arches={args.arch})

    # Write json
    logger.info("Writing JSON")
    with open(args.output, "w") as file:
        file.write(RootModel[CharmhubMap](charmhub_map).model_dump_json())


if __name__ == "__main__":
    main()
