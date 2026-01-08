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


import dataclasses
import heapq
import logging
import random

from .bundle import Application, ApplicationEndpoint, Bundle, Integration
from .charm import ENDPOINT_PROVIDES, ENDPOINT_REQUIRES, CharmConfig
from .charmhub import CharmhubClient
from .immutable_dataclass import computed_property, immutable_dataclass


@immutable_dataclass
class Node:
    bundle: Bundle
    aggression: float

    @computed_property
    def score(self):
        # Prioritize fewer applications, accounting for charm priorities
        weight_applications = sum(1.0 / app.charm.priority for app in self.bundle.applications)
        # Prioritize fewer unfulfilled endpoints
        weight_unfulfilled_endpoints = self.aggression * len(self.bundle.unfulfilled_endpoints)
        # Prioritize more integrations, scaled by aggression
        # As aggression increases (expected to be between 0 and 1) the weight of integrations increases
        # This forces the algorithm to explore deeper (DFS) rather than wider (BFS) into the graph in order to find a solution sooner as aggression is increased
        weight_integrations = self.aggression * len(self.bundle.integrations) * -1
        # Sum the weights to get the final score
        return weight_applications + weight_unfulfilled_endpoints + weight_integrations

    @computed_property
    def fingerprint(self) -> frozenset[Integration]:
        return self.bundle.integrations

    @computed_property
    def stats(self) -> str:
        return f"{len(self.bundle.applications)} applications ({len(self.bundle.unfulfilled_endpoints)} unfulfilled endpoints, {len(self.bundle.saturated_endpoints)} saturated endpoints)"

    def __lt__(self, other):
        return self.score < other.score


class BundleBuilder:
    charmhub_client: CharmhubClient
    logger: logging.Logger
    max_nodes_visited: int | None
    aggression_limit: int
    aggression_interval: int
    avoid_application_dependency_cycles: bool

    def __init__(
        self,
        charmhub_client: CharmhubClient,
        logger: logging.Logger = logging.getLogger(__name__),
        max_nodes_visited: int | None = None,
        aggression_limit: int = 50000,
        aggression_interval: int = 5000,
        avoid_application_dependency_cycles: bool = False,
    ):
        self.charmhub_client = charmhub_client
        self.logger = logger
        self.max_nodes_visited = max_nodes_visited
        self.aggression_limit = aggression_limit
        self.aggression_interval = aggression_interval
        self.avoid_application_dependency_cycles = avoid_application_dependency_cycles

    # Build out the bundle, pulling in charms that fulfill non-optional hanging required integrations
    def build(self, base: Bundle) -> Bundle:
        # This follows a rough uniform cost algorithm
        queued_nodes = [
            Node(
                bundle=base,
                aggression=0.0,
            )
        ]
        best_node = queued_nodes[0]
        known_nodes = {best_node.fingerprint}
        self.logger.info(f"Starting with bundle: {best_node.stats}")
        while len(queued_nodes) > 0:
            # Rebalance node scores
            num_visited_nodes = len(known_nodes) - len(queued_nodes)
            if num_visited_nodes % self.aggression_interval == 0 and num_visited_nodes <= self.aggression_limit:
                aggression = 1.0 - max((self.aggression_limit - num_visited_nodes) / self.aggression_limit, 0.0)
                best_node = dataclasses.replace(best_node, aggression=aggression)
                queued_nodes = [dataclasses.replace(node, aggression=aggression) for node in queued_nodes]
                heapq.heapify(queued_nodes)

            # Get node with with the best score from the sorted queue
            node = heapq.heappop(queued_nodes)
            self.logger.debug(
                f"Checking bundle: {node.stats}, visited nodes: {num_visited_nodes}, queued nodes: {len(queued_nodes)}"
            )

            # If this is the best node we've seen note it
            if node < best_node:
                self.logger.info(f"New best bundle: {node.stats}")
                best_node = node

            # If we've reached the maximum number of visited nodes stop
            if self.max_nodes_visited is not None and num_visited_nodes >= self.max_nodes_visited:
                self.logger.info("Reached maximum number of visited nodes, stopping")
                break

            # Get the child nodes
            child_nodes = self.child_nodes(node)

            # If there are no child nodes quit now
            # No more child nodes means no further integrations can be added
            if len(child_nodes) == 0:
                self.logger.info("Node has no more child nodes, stopping")
                best_node = node
                break

            # Add this node's children to the sorted queue
            for child_node in child_nodes:
                if child_node.fingerprint in known_nodes:
                    continue
                known_nodes.add(child_node.fingerprint)
                heapq.heappush(queued_nodes, child_node)

        # Pick the best bundle
        best_bundle = best_node.bundle

        # Note unresolved endpoints
        for application_endpoint in best_bundle.unfulfilled_endpoints:
            self.logger.warning(f"Cannot resolve application endpoint: {application_endpoint}")

        # Resolve test configs
        best_bundle = self.add_test_configs(best_bundle)

        # Return best bundle
        return best_bundle

    def child_nodes(self, node: Node) -> set[Node]:
        # Check each unfulfilled endpoint
        child_nodes: set[Node] = set()
        for unfulfilled_application_endpoint in node.bundle.unfulfilled_endpoints:
            # Get all possible child nodes by integrating with existing_applications
            child_nodes |= self.child_nodes_existing_applications(node, unfulfilled_application_endpoint)

            # Get all possible child nodes by adding and integrating with new applications
            child_nodes |= self.child_nodes_new_applications(node, unfulfilled_application_endpoint)

        return child_nodes

    def child_nodes_existing_applications(
        self, node: Node, unfulfilled_application_endpoint: ApplicationEndpoint
    ) -> set[Node]:
        # Check all potential application endpoints to see if they can fulfill the unfulfilled endpoint
        child_nodes: set[Node] = set()
        for possible_application_endpoint in node.bundle.application_endpoints:
            # Get charm endpoints
            unfulfilled_charm_endpoint = node.bundle.application_endpoints[unfulfilled_application_endpoint]
            possible_charm_endpoint = node.bundle.application_endpoints[possible_application_endpoint]

            # Will not integrate same application
            if possible_application_endpoint.application == unfulfilled_application_endpoint.application:
                continue

            # Will not integrate different interfaces
            if possible_charm_endpoint.interface != unfulfilled_charm_endpoint.interface:
                continue

            # Will not integrate wrong endpoint types
            if (
                possible_charm_endpoint.type == ENDPOINT_REQUIRES
                and unfulfilled_charm_endpoint.type == ENDPOINT_PROVIDES
            ):
                require_endpoint = possible_charm_endpoint
                provide_endpoint = unfulfilled_charm_endpoint
            elif (
                possible_charm_endpoint.type == ENDPOINT_PROVIDES
                and unfulfilled_charm_endpoint.type == ENDPOINT_REQUIRES
            ):
                require_endpoint = unfulfilled_charm_endpoint
                provide_endpoint = possible_charm_endpoint
            else:
                continue

            # Will not integrate require with provide that does not provide features
            if not provide_endpoint.features >= require_endpoint.features:
                continue

            # Will not integrate if it exceeds limit
            if possible_application_endpoint in node.bundle.saturated_endpoints:
                continue

            # Will not integrate applications that would create a cycle
            if self.avoid_application_dependency_cycles:
                if unfulfilled_charm_endpoint.type == ENDPOINT_REQUIRES:
                    requiring_application = unfulfilled_application_endpoint.application
                    providing_application = possible_application_endpoint.application
                else:
                    requiring_application = possible_application_endpoint.application
                    providing_application = unfulfilled_application_endpoint.application
                if node.bundle.has_application_dependency(providing_application, requiring_application):
                    continue

            # Will not integrate if it creates a recursive dependency chain
            if node.bundle.has_endpoint_dependency(
                unfulfilled_application_endpoint.application,
                node.bundle.application_lookup[possible_application_endpoint.application].charm.name,
                possible_charm_endpoint.name,
                possible_charm_endpoint.type,
            ):
                continue

            # Add this as a child node
            child_nodes.add(
                Node(
                    bundle=dataclasses.replace(
                        node.bundle,
                        integrations=node.bundle.integrations
                        | {Integration({possible_application_endpoint, unfulfilled_application_endpoint})},
                    ),
                    aggression=node.aggression,
                )
            )

        return child_nodes

    def child_nodes_new_applications(
        self, node: Node, unfulfilled_application_endpoint: ApplicationEndpoint
    ) -> set[Node]:
        # Get the charm endpoint
        unfulfilled_charm_endpoint = node.bundle.application_endpoints[unfulfilled_application_endpoint]

        # Find fulfilling charms
        fulfilling_charms: set[str] = set()
        if unfulfilled_charm_endpoint.type == ENDPOINT_REQUIRES:
            fulfilling_charms |= self.charmhub_client.find_charms(
                provides=unfulfilled_charm_endpoint.interface, platform=node.bundle.platform
            )
        if unfulfilled_charm_endpoint.type == ENDPOINT_PROVIDES:
            fulfilling_charms |= self.charmhub_client.find_charms(
                requires=unfulfilled_charm_endpoint.interface, platform=node.bundle.platform
            )

        # Check each fulfilling charm to see if it can integrate
        child_nodes: set[Node] = set()
        for charm_name in fulfilling_charms:
            # Get the default charm release
            charm = self.charmhub_client.charm_from_store(charm_name=charm_name, ubuntu_arch=node.bundle.arch)

            # Create the application
            application = Application(
                name=node.bundle.generate_unique_application_name(charm_name),
                charm=charm,
            )

            # Check each endpoint on the charm to see if it can fulfill the unfulfilled endpoint
            for possible_charm_endpoint in application.charm.endpoints:
                # Will not integrate different interfaces
                if possible_charm_endpoint.interface != unfulfilled_charm_endpoint.interface:
                    continue

                # Will not integrate wrong endpoint types
                if (
                    possible_charm_endpoint.type == ENDPOINT_REQUIRES
                    and unfulfilled_charm_endpoint.type == ENDPOINT_PROVIDES
                ):
                    require_endpoint = possible_charm_endpoint
                    provide_endpoint = unfulfilled_charm_endpoint
                elif (
                    possible_charm_endpoint.type == ENDPOINT_PROVIDES
                    and unfulfilled_charm_endpoint.type == ENDPOINT_REQUIRES
                ):
                    require_endpoint = unfulfilled_charm_endpoint
                    provide_endpoint = possible_charm_endpoint
                else:
                    continue

                # Will not integrate require with provide that does not provide features
                if not provide_endpoint.features >= require_endpoint.features:
                    continue

                # Will not integrate if it creates a recursive dependency chain
                if node.bundle.has_endpoint_dependency(
                    unfulfilled_application_endpoint.application,
                    charm_name,
                    possible_charm_endpoint.name,
                    possible_charm_endpoint.type,
                ):
                    continue

                # Add as a valid child node
                child_nodes.add(
                    Node(
                        bundle=dataclasses.replace(
                            node.bundle,
                            applications=node.bundle.applications | {application},
                            integrations=node.bundle.integrations
                            | {
                                Integration(
                                    {
                                        unfulfilled_application_endpoint,
                                        ApplicationEndpoint(
                                            application=application.name, endpoint=possible_charm_endpoint.name
                                        ),
                                    }
                                )
                            },
                        ),
                        aggression=node.aggression,
                    )
                )

        return child_nodes

    @staticmethod
    def add_test_configs(bundle: Bundle) -> Bundle:
        applications: set[Application] = set()
        for application in bundle.applications:
            possible_configs: list[CharmConfig] = []
            for test_config in application.charm.test_configs:
                if test_config.criteria.valid(
                    channel=application.charm.channel,
                    integrated_endpoints=bundle.application_to_integrated_endpoints[application.name],
                ):
                    possible_configs.append(test_config.config)

            # If there are not test configs defined return empty
            if len(possible_configs) == 0:
                possible_configs = [CharmConfig()]

            # Pick a random config
            # This function is not secure in cryptography, but should be fine to use here
            applications.add(dataclasses.replace(application, config=random.choice(possible_configs)))  # nosec B311

        return dataclasses.replace(bundle, applications=frozenset(applications))
