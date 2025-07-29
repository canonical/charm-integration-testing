# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse
import json
import os

import matplotlib.cm as cm
import matplotlib.colors as colors
import matplotlib.pyplot as plt
import networkx as nx

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
    parser.add_argument(
        "--output-directory",
        type=str,
        help="Directory to store graphs in",
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

    # Make output directory
    os.makedirs(args.output_directory, exist_ok=True)

    # Create graphs
    for charm in charmhub_map.charms:
        for platform in charm.platforms:
            for arch in platform.arches:
                for version in arch.versions:
                    visited_nodes = version.minimal_bundle_nodes

                    print(f"{version.version.name}: {len(visited_nodes)}")

                    graph = nx.Graph()

                    for node in visited_nodes:
                        graph.add_node(node.fingerprint)
                        if node.parent:
                            graph.add_edge(node.parent.fingerprint, node.fingerprint)

                    norm = colors.Normalize(vmin=0, vmax=len(visited_nodes) - 1)
                    cmap = cm.get_cmap("coolwarm")
                    color_map = [cmap(norm(i)) for i, _ in enumerate(visited_nodes)]
                    color_map[0] = "black"
                    color_map[-1] = "green"

                    pos = nx.spring_layout(graph, seed=42)

                    nx.draw(
                        graph,
                        pos,
                        with_labels=False,
                        node_color=color_map,
                        node_size=10,
                        edge_color="gray",
                        width=0.2,
                        alpha=0.5,
                    )

                    plt.savefig(f"{args.output_directory}/{version.version.name}.png", dpi=300)

                    plt.clf()


if __name__ == "__main__":
    main()
