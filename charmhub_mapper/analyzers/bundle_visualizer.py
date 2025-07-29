# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse
import json
import os
from collections import defaultdict

import matplotlib.pyplot as plt
import networkx as nx

from bundle_builder import ENDPOINT_REQUIRES
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
    parser.add_argument(
        "--output-type",
        type=str,
        help="Type of file for graphs",
        choices=["png", "svg"],
        default="png",
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
                    bundle = version.minimal_bundle

                    graph = nx.MultiDiGraph()

                    for application in bundle.applications:
                        graph.add_node(application.name)

                    for integration in bundle.integrations:
                        endpoint_1, endpoint_2 = integration
                        if bundle.application_endpoints[endpoint_1].type == ENDPOINT_REQUIRES:
                            requirer = endpoint_1.application
                            provider = endpoint_2.application
                        else:
                            requirer = endpoint_2.application
                            provider = endpoint_1.application
                        graph.add_edge(provider, requirer, label=bundle.application_endpoints[endpoint_1].interface)

                    color_map = ["red" if node == "target" else "skyblue" for node in graph.nodes()]

                    pos = nx.shell_layout(graph)

                    application_to_charm = {
                        application.name: application.charm.name for application in bundle.applications
                    }
                    labels = {}
                    label_pos = {}
                    for application in graph.nodes():
                        labels[application] = application_to_charm[application]
                        label_pos[application] = pos[application]

                    plt.axis("off")
                    plt.margins(x=0.2, y=0.2)
                    nx.draw_networkx_nodes(graph, pos, node_color=color_map, node_size=3500)
                    nx.draw_networkx_labels(graph, label_pos, labels=labels, font_size=6)

                    total_counts = defaultdict(int)
                    for u, v, _ in graph.edges(keys=True):
                        total_counts[(u, v)] += 1
                    for u, v, k in graph.edges(keys=True):
                        connectionstyle = f"arc3,rad={0.15*total_counts[(u, v)]}"
                        nx.draw_networkx_edges(
                            graph, pos, edgelist=[(u, v)], node_size=3500, connectionstyle=connectionstyle
                        )
                        nx.draw_networkx_edge_labels(
                            graph,
                            pos,
                            {(u, v, k): graph.edges[(u, v, k)]["label"]},
                            connectionstyle=connectionstyle,
                            font_size=6,
                        )
                        total_counts[(u, v)] -= 1

                    if args.output_type == "svg":
                        plt.savefig(f"{args.output_directory}/{version.version.name}.svg", bbox_inches="tight")
                    else:
                        plt.savefig(f"{args.output_directory}/{version.version.name}.png", dpi=300)

                    plt.clf()


if __name__ == "__main__":
    main()
