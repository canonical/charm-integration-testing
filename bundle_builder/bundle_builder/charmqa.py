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

import logging
from collections import defaultdict
from pathlib import Path

import requests
import yaml

from .charm import Charm, NoCharmMetadataException


class BasicFIFO:
    def __init__(self):
        self.items = []

    def append(self, item):
        self.items.append(item)

    def get_all(self):
        # Make a shallow copy
        output = self.items[:]
        self.items = []
        return output

    def pop(self):
        # Shallow copy and then delete last element
        output = self.items[:]
        output = output[-1]
        self.items = self.items[:-1]
        return output

    def empty(self):
        return len(self.items) == 0


# Function to find 1-to-1 matches
def find_relations(items):
    matches = []

    for item in items:
        for output in item.provides_integrations:
            for target_item in items:
                if target_item != item:
                    for input in target_item.requires_integrations:
                        if output["interface"] == input["interface"]:
                            matches.append(
                                [
                                    f"{item.name}:{output['endpoint_name']}",
                                    f"{target_item.name}:{input['endpoint_name']}",
                                ]
                            )
    return matches


def find_interface_providers(interface_name, logger=logging.getLogger("bundle_builder")):
    output = []
    CHARM_STORE_JSON_ENDPOINT = f"https://charmhub.io/store.json?size=300&provides={interface_name}"
    resp = requests.get(url=CHARM_STORE_JSON_ENDPOINT, timeout=180)
    resp.raise_for_status()
    resp_data = resp.json()
    for provider_charm in resp_data["packages"]:
        try:
            output.append(Charm.from_store_default(charm_name=provider_charm["package"]["name"], logger=logger))
        except NoCharmMetadataException:
            # Lets silently fail for now
            pass

    return output


def build_charm_graph(root_charm, max_depth=3, logger=logging.getLogger("bundle_builder")):
    iterations = 0
    control_stack = BasicFIFO()
    control_stack.append(root_charm)
    visited_nodes = set()
    graph_output = defaultdict(list)

    while not control_stack.empty() and iterations < max_depth:
        target_charms = control_stack.get_all()
        logger.debug(
            f"Iteration: #{iterations}; visted: {list(visited_nodes)}; iterstack: {target_charms}; control stack: {control_stack.items}"
        )
        for target_charm in target_charms:
            if target_charm.name in visited_nodes:
                continue
            logger.info("{}[C] {}".format('\t'*(iterations), target_charm.name))
            for target_charm_integration in target_charm.non_optional_requires:
                for interface_provider in find_interface_providers(
                    target_charm_integration["interface"], logger=logger
                ):
                    logger.info(
                        "{}[I] {}:{}".format('\t'*(iterations+1), target_charm_integration['interface'], interface_provider.name)
                    )
                    graph_output[target_charm].append((target_charm_integration["interface"], interface_provider))
                    if interface_provider.name not in visited_nodes:
                        control_stack.append(interface_provider)
            visited_nodes.add(target_charm.name)
        iterations += 1

    return graph_output


def find_all_paths(graph, root_node):
    """
    Finds all paths from each first-level node in the graph, respecting relationships.

    Parameters:
        graph (dict): A dictionary representing the graph, where each key is a node,
                      and its value is a list of tuples (relationship, neighbor).

    Returns:
        dict: A dictionary where the keys are the first-level nodes, and the values are
              lists of paths. Each path is represented as a list of tuples
              [(node, relationship, neighbor), ...].
    """

    def dfs(node, path, paths):
        # If the current node has no outgoing edges, add the current path
        if node not in graph or not graph[node]:
            paths.append(path)
            return

        # Explore all neighbors recursively
        for relationship, neighbor in graph[node]:
            dfs(neighbor, path + [(node, relationship, neighbor)], paths)

    paths = []
    dfs(root_node, [], paths)

    return paths


def group_paths_by_interface(graph_paths):
    grouped_paths = defaultdict(list)
    for path in graph_paths:
        first_interface = path[0][1]
        grouped_paths[first_interface].append(path)
    return grouped_paths


def collapse_path(expanded_path):
    result = []
    for source, _, target in expanded_path:
        # Add the source if it's the first element or different from the previous target
        if not result or result[-1] != source:
            result.append(source)
        result.append(target)
    return result


def select_shortest_path(possible_paths, charm_bias=None, logger=logging.getLogger("bundle_builder")):
    def filter_criteria(value, int_charm_bias=charm_bias):
        return any([c.is_equal(value) for c in int_charm_bias])

    if charm_bias:
        if isinstance(charm_bias, Charm):
            # Convert it to a list
            charm_bias = [charm_bias]

    possible_paths = [collapse_path(p) for p in possible_paths]
    filtered_paths = []
    # Collapse all paths first
    for path in possible_paths:
        if any([filter_criteria(node, charm_bias) for node in path]):
            filtered_paths.append(path)

    if len(filtered_paths) < 1:
        # All paths got filtered, means our bias cannot be fullfiled, thus we allow to use any path
        # XXX: Maybe return empty instead?
        # return []
        logger.warning("All paths got filtered by the bias. The bias cannot be fulfilled. Returning all paths.")
        filtered_paths = possible_paths

    shortest_path = filtered_paths.pop(0)
    for path in filtered_paths:
        # Find shortest path
        if len(path) < len(shortest_path):
            shortest_path = path
    return shortest_path


def filter_to_shortest_paths(target, grouped_paths, support_charms, logger=logging.getLogger("bundle_builder")):
    selected_paths = {}
    logger.debug(f"Shortest paths from {target}:")
    logger.debug(f"[C] {target}")
    for interface, possible_paths in grouped_paths.items():
        shortest_path = select_shortest_path(possible_paths=possible_paths, charm_bias=support_charms)
        selected_paths[interface] = shortest_path
        logger.debug(f"\t[I] {interface}: {shortest_path}")

    return selected_paths


def render_all_generated_bundles(selected_paths, logger=logging.getLogger("bundle_builder")):
    rendered_bundles = {}
    for interface, selected_path in selected_paths.items():
        relations = find_relations(selected_path)

        charms_apps = {}
        for charm in selected_path:
            charms_apps[charm.name] = {"charm": charm.name, "channel": charm.channel, "scale": 1}

        bundle = {"bundle": "kubernetes", "applications": {**charms_apps}, "relations": [*relations]}

        rendered_bundles[interface] = bundle

    return rendered_bundles


def dump_selected_bundle_to_file(
    rendered_bundles: str, selected_interface: str, filename: str, logger=logging.getLogger("bundle_builder")
):
    path = Path(filename).absolute().resolve()

    with open(path, "w", encoding="utf-8") as f:
        target_bundle = rendered_bundles.get(selected_interface)
        target_bundle_yaml = yaml.dump(target_bundle)
        logger.debug(f"Generated bundle for interface {selected_interface}: ")
        logger.debug(target_bundle_yaml)
        logger.debug(f"Saving bundle to {path}")
        f.write(target_bundle_yaml)
        f.flush()
