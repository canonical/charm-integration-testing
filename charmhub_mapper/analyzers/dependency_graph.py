# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import argparse
import csv
import json
from pathlib import Path

import matplotlib.pyplot as plt
import networkx as nx
import yaml
from matplotlib.colors import LinearSegmentedColormap

from bundle_builder import ENDPOINT_PROVIDES, ENDPOINT_REQUIRES
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
        "--output",
        type=str,
        help="File to store graph",
        default="dependency-graph.png",
    )
    parser.add_argument(
        "--layout",
        help="Layout nodes in a circle",
        choices=[
            "circle",
            "spring",
            "kamada_kawai",
        ],
        default="spring",
    )
    parser.add_argument(
        "--include-optional",
        help="Include optional integrations",
        action="store_true",
    )
    parser.add_argument(
        "--include-labels",
        help="How many of the most common nodes to include",
        type=int,
        default=0,
    )
    parser.add_argument(
        "--test-executions",
        help="Test observer test executions CSV",
        required=False,
    )
    parser.add_argument(
        "--generated-bundles",
        help="Directory containing generated bundles for test executions",
        required=False,
    )
    parser.add_argument(
        "--indicate-test-results",
        help="Include optional integrations",
        action="store_true",
    )
    args = parser.parse_args()
    if args.indicate_test_results:
        if not args.test_executions:
            parser.error("--test-executions is required when --indicate-test-results is provided")
        if not args.generated_bundles:
            parser.error("--generated-bundles is required when --indicate-test-results is provided")
    return args


def main():
    # Parse args
    args = get_args()

    # Get logger
    logger = get_logger("dependency_graph", args.debug)

    # Load charmhub map
    logger.info("Loading Charmhub map")
    with open(args.map_file, "r") as file:
        charmhub_map = CharmhubMap(**json.load(file))

    # Gather versions
    versions = [
        version.version
        for charm in charmhub_map.charms
        for platform in charm.platforms
        for arch in platform.arches
        for version in arch.versions
    ]

    graph = nx.Graph()

    # Add edges
    for version in versions:
        for require in version.endpoints:
            if require.type != ENDPOINT_REQUIRES:
                continue
            if not args.include_optional and require.optional:
                continue

            for other in versions:
                for provide in other.endpoints:
                    if version.name == other.name:
                        continue
                    if provide.type != ENDPOINT_PROVIDES:
                        continue
                    if provide.interface != require.interface:
                        continue

                    charm_1, charm_2 = sorted({version.name, other.name})
                    graph.add_node(charm_1, passes=0, failures=0, occurrences=0)
                    graph.add_node(charm_2, passes=0, failures=0, occurrences=0)
                    graph.add_edge(charm_1, charm_2, passes=0, failures=0, occurrences=0)

    # Calculate edge weights and colors using test executions
    with open(args.test_executions, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["Artefact.family"] != "charm":
                continue

            # Get integrations from this test execution
            integrations = set()
            test_plan_attributes = row["TestExecution.test_plan"].split("/")
            integrations.add(frozenset({test_plan_attributes[1].split(":")[0], test_plan_attributes[3].split(":")[0]}))

            # Get integrations from bundle
            bundle_path = Path(args.generated_bundles) / f"{row['TestExecution.id']}.yaml"
            if bundle_path.exists():
                logger.debug(f"Check integrations in bundle {row['TestExecution.id']}.yaml")
                with bundle_path.open("r") as f:
                    bundle = yaml.safe_load(f)
                    for relation in bundle["relations"]:
                        integration = set()
                        for endpoint in relation:
                            application = endpoint.split(":")[0]
                            charm = bundle["applications"][application]["charm"]
                            integration.add(charm)

            # Check every integration
            for integration in integrations:
                if len(integration) == 1:
                    continue
                charm_1, charm_2 = integration
                if not graph.has_edge(charm_1, charm_2):
                    continue
                graph[charm_1][charm_2]["occurrences"] += 1
                if row["TestExecution.status"] == "PASSED":
                    graph[charm_1][charm_2]["passes"] += 1
                if row["TestExecution.status"] == "FAILED":
                    graph[charm_1][charm_2]["failures"] += 1

            # Add occurrences to node
            for charm in {charm for integration in integrations for charm in integration}:
                if charm not in graph.nodes:
                    continue
                graph.nodes[charm]["occurrences"] += 1
                if row["TestExecution.status"] == "PASSED":
                    graph.nodes[charm]["passes"] += 1
                if row["TestExecution.status"] == "FAILED":
                    graph.nodes[charm]["failures"] += 1

    # Calculate edge colors
    if args.indicate_test_results:
        # cmap = plt.cm.RdYlGn
        cmap = LinearSegmentedColormap.from_list("SoftRedGreen", ["#ff5555", "yellow", "green"])
        edge_colors = []
        for charm_1, charm_2 in graph.edges():
            passes = graph[charm_1][charm_2]["passes"]
            failures = graph[charm_1][charm_2]["failures"]
            total = passes + failures
            if total == 0:
                edge_colors.append("gray")
                continue
            edge_colors.append(cmap(passes / total))
    else:
        edge_colors = "gray"

    # Calculate edge thickness
    if args.indicate_test_results:
        edge_thickness = [graph[charm_1][charm_2]["occurrences"] for charm_1, charm_2 in graph.edges()]
        min_occurrences = min(edge_thickness)
        max_occurrences = max(edge_thickness)
        min_size = 0.75
        max_size = 3.0
        edge_thickness = [
            (occurrences - min_occurrences) / (max_occurrences - min_occurrences) * (max_size - min_size) + min_size
            for occurrences in edge_thickness
        ]
    else:
        edge_thickness = 0.2

    # Calculate node size
    if args.indicate_test_results:
        node_size = [graph.nodes[charm]["occurrences"] for charm in graph.nodes()]
        min_occurrences = min(node_size)
        max_occurrences = max(node_size)
        min_size = 2
        max_size = 50
        node_size = [
            (occurrences - min_occurrences) / (max_occurrences - min_occurrences) * (max_size - min_size) + min_size
            for occurrences in node_size
        ]
    else:
        node_size = (10,)

    # Calculate node colors
    if args.indicate_test_results:
        cmap = LinearSegmentedColormap.from_list("SoftRedGreen", ["#ff5555", "yellow", "green"])
        node_colors = []
        for charm in graph.nodes():
            passes = 0
            failures = 0
            passes = graph.nodes[charm]["passes"]
            failures = graph.nodes[charm]["failures"]
            total = passes + failures
            if total == 0:
                node_colors.append("gray")
                continue
            node_colors.append(cmap(passes / total))
    else:
        node_colors = "#333333"

    # Print largest nodes
    if args.indicate_test_results:
        for charm, occurrences in reversed(
            sorted({(charm, graph.nodes[charm]["occurrences"]) for charm in graph.nodes()}, key=lambda x: x[1])
        ):
            logger.debug(f"charm {charm}: occurrences: {occurrences}")

    logger.info(f"Number of charms (nodes): {len(graph.nodes())}")
    logger.info(f"Number of integrations (edges): {len(graph.edges())}")

    # Determine layout
    if args.layout == "circle":
        pos = nx.shell_layout(graph)
    elif args.layout == "kamada_kawai":
        pos = nx.kamada_kawai_layout(graph)
    else:
        pos = nx.spring_layout(graph, seed=42)

    # Calculate labels
    node_to_size = {node: size for node, size in zip(graph.nodes(), node_size)}
    labels = {}
    label_pos = {}
    for charm in list(reversed(sorted(graph.nodes(), key=lambda charm: graph.nodes[charm]["occurrences"])))[
        0 : args.include_labels
    ]:
        labels[charm] = charm
        x, y = pos[charm]
        label_pos[charm] = (x, y - 0.02 - 0.0003 * node_to_size[charm])

    # Draw graph
    # nx.draw(graph,pos,node_color="#333333",node_size=node_size,alpha=0.5)
    plt.axis("off")
    nx.draw_networkx_nodes(graph, pos, node_color=node_colors, node_size=node_size, alpha=0.5)
    nx.draw_networkx_edges(graph, pos, edge_color=edge_colors, width=edge_thickness, alpha=0.1)
    nx.draw_networkx_labels(graph, label_pos, labels=labels, font_size=4)

    if args.output.endswith("svg"):
        plt.savefig(args.output, bbox_inches="tight")
    else:
        plt.savefig(args.output, dpi=300)

    plt.clf()


if __name__ == "__main__":
    main()
