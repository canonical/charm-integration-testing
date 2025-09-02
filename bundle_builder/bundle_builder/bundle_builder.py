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
from .charm import ENDPOINT_PEERS, ENDPOINT_PROVIDES, ENDPOINT_REQUIRES, CharmConfig
from .charmhub import CharmhubClient
from .immutable_dataclass import computed_property, immutable_dataclass


@immutable_dataclass
class Node:
    bundle: Bundle
    application_endpoint_to_possible_charm: frozenset[tuple[ApplicationEndpoint, str]]
    balance: float

    @computed_property
    def fulfillable_interfaces(self) -> frozenset[str]:
        return frozenset(
            {
                self.bundle.application_endpoints[application_endpoint].interface
                for application_endpoint, _ in self.application_endpoint_to_possible_charm
            }
        )

    @computed_property
    def score(self) -> float:
        # balance changes the weight prioritizing number of applications over unfulfilled interfaces
        # it is expected to be between 0 and 1, where 1 prioritizes the smallest bundle
        return self.balance * len(self.bundle.applications) + (1.0 - self.balance) * len(self.fulfillable_interfaces)

    @computed_property
    def fingerprint(self) -> frozenset[str]:
        return self.bundle.charms

    @computed_property
    def child_charms(self) -> frozenset[str]:
        return frozenset({charm_name for _, charm_name in self.application_endpoint_to_possible_charm})

    @computed_property
    def stats(self) -> str:
        return f"{len(self.bundle.applications)} applications ({len(self.bundle.unfulfilled_interfaces)} unfulfilled and {len(self.fulfillable_interfaces)} fulfillable interfaces)"

    def __lt__(self, other):
        return self.score < other.score


class BundleBuilder:
    charmhub_client: CharmhubClient
    logger: logging.Logger
    max_nodes_visited: int = 50000
    rebalance_interval: int = 1000

    def __init__(self, charmhub_client: CharmhubClient, logger=logging.getLogger(__name__)):
        self.charmhub_client = charmhub_client
        self.logger = logger

    # Build out the bundle, pulling in charms that fulfill non-optional hanging required integrations
    def build(self, base: Bundle) -> Bundle:
        # This follows a rough uniform cost algorithm
        queued_nodes = [self.new_node(base)]
        best_node = queued_nodes[0]
        known_nodes = {best_node.fingerprint}
        self.logger.info(f"Starting with bundle: {best_node.stats}")
        while len(queued_nodes) > 0:
            # Rebalance node scores
            num_visited_nodes = len(known_nodes) - len(queued_nodes)
            if num_visited_nodes % self.rebalance_interval == 0:
                balance = max((self.max_nodes_visited - num_visited_nodes) / self.max_nodes_visited, 0)
                queued_nodes = [dataclasses.replace(node, balance=balance) for node in queued_nodes]
                heapq.heapify(queued_nodes)

            # Get node with with the best score from the sorted queue
            node = heapq.heappop(queued_nodes)
            self.logger.debug(
                f"Checking bundle: {node.stats}, visited nodes: {num_visited_nodes}, queued nodes: {len(queued_nodes)}"
            )

            # If there are no fulfillable interfaces quit now
            # We could exhaustively search the graph but that comes at the cost of compute
            if len(node.fulfillable_interfaces) == 0:
                self.logger.info("Bundle has no more fulfillable interfaces, stopping")
                best_node = node
                break

            # Add this node's children to the sorted queue
            for child_node in self.child_nodes(node):
                if child_node.fingerprint not in known_nodes:
                    known_nodes.add(child_node.fingerprint)
                    heapq.heappush(queued_nodes, child_node)

        # Note unresolved endpoints
        for application_endpoint in best_node.bundle.unfulfilled_endpoints:
            self.logger.warning(f"Cannot resolve application endpoint: {application_endpoint}")

        # Return best node
        return best_node.bundle

    def add_missing_integrations(self, bundle: Bundle) -> Bundle:
        # Start over whenever a new integration is added
        while True:
            start_over = False

            # Check all unfulfilled endpoints to see if they are fulfillable
            for unfulfilled_application_endpoint in bundle.unfulfilled_endpoints:
                unfulfilled_charm_endpoint = bundle.application_endpoints[unfulfilled_application_endpoint]

                # Check all potential application endpoints to see if they can fulfill the unfulfilled endpoint
                for possible_application_endpoint, possible_charm_endpoint in bundle.application_endpoints.items():
                    # Will not integrate with self
                    if possible_application_endpoint.application == unfulfilled_application_endpoint.application:
                        continue
                    # Will not integrate different interfaces
                    if possible_charm_endpoint.interface != unfulfilled_charm_endpoint.interface:
                        continue
                    # Will not integrate wrong endpoint types
                    if not (
                        (
                            possible_charm_endpoint.type == ENDPOINT_REQUIRES
                            and unfulfilled_charm_endpoint.type == ENDPOINT_PROVIDES
                        )
                        or (
                            possible_charm_endpoint.type == ENDPOINT_PROVIDES
                            and unfulfilled_charm_endpoint.type == ENDPOINT_REQUIRES
                        )
                    ):
                        continue

                    # Check limit constraints - skip if either endpoint would exceed its limit
                    connection_counts = bundle.endpoint_connection_counts
                    unfulfilled_current_connections = connection_counts.get(unfulfilled_application_endpoint, 0)
                    possible_current_connections = connection_counts.get(possible_application_endpoint, 0)
                    
                    if (unfulfilled_charm_endpoint.limit is not None and 
                        unfulfilled_current_connections >= unfulfilled_charm_endpoint.limit):
                        self.logger.debug(
                            f"Skipping integration: {unfulfilled_application_endpoint} has reached limit "
                            f"({unfulfilled_current_connections}/{unfulfilled_charm_endpoint.limit})"
                        )
                        continue
                    
                    if (possible_charm_endpoint.limit is not None and 
                        possible_current_connections >= possible_charm_endpoint.limit):
                        self.logger.debug(
                            f"Skipping integration: {possible_application_endpoint} has reached limit "
                            f"({possible_current_connections}/{possible_charm_endpoint.limit})"
                        )
                        continue

                    # Integration is good, add and start again
                    bundle = Bundle(
                        applications=bundle.applications,
                        integrations=frozenset(
                            bundle.integrations
                            | {Integration({unfulfilled_application_endpoint, possible_application_endpoint})}
                        ),
                        platform=bundle.platform,
                        arch=bundle.arch,
                    )

                    # Start over with new bundle
                    start_over = True
                    break

                # Start again
                if start_over:
                    break
            else:
                # No more integrations can be fulfilled
                return bundle

    # Return a new node, including the possible child charms
    def new_node(self, bundle: Bundle, balance: float = 1.0) -> Node:
        # Ensure all possible integrations are fulfilled by the bundle
        bundle = self.add_missing_integrations(bundle)

        # Get all possible fulfillments for unfulfilled endpoints
        application_endpoint_to_possible_charm = set()
        for application_endpoint in bundle.unfulfilled_endpoints:
            # Get the charm endpoint
            charm_endpoint = bundle.application_endpoints[application_endpoint]

            # Find fulfilling charms
            fulfilling_charms = set()
            if charm_endpoint.type == ENDPOINT_REQUIRES:
                fulfilling_charms = self.charmhub_client.find_charms(
                    provides=charm_endpoint.interface, platform=bundle.platform
                )
            if charm_endpoint.type == ENDPOINT_PROVIDES:
                fulfilling_charms = self.charmhub_client.find_charms(
                    requires=charm_endpoint.interface, platform=bundle.platform
                )

            # Explicitly remove the bundle charms as we cannot use a charm in the bundle to fulfill an unfulfillable interface
            # An example is grafana-agent-k8s provides and requires `tracing`, and is the only charm in Charmhub to use `tracing`
            fulfilling_charms -= bundle.charms

            # Save mappings
            application_endpoint_to_possible_charm |= {(application_endpoint, charm) for charm in fulfilling_charms}

        # Handle special injection cases for endpoints that have reached their limits
        application_endpoint_to_possible_charm |= self._get_injected_charm_fulfillments(bundle)

        # Return node
        return Node(
            bundle=bundle,
            application_endpoint_to_possible_charm=frozenset(application_endpoint_to_possible_charm),
            balance=balance,
        )

    # Each child node is the addition of an application to the bundle that fulfills a
    # missing non-optional required endpoint
    def child_nodes(self, node: Node) -> frozenset[Node]:
        # Get the default release for all the child charms
        child_charms = {
            self.charmhub_client.charm_from_store(charm_name=charm_name, ubuntu_arch=node.bundle.arch)
            for charm_name in node.child_charms
        }

        # Create a child node for each child charm
        return frozenset(
            {
                self.new_node(
                    bundle=Bundle(
                        applications=frozenset(
                            node.bundle.applications
                            | {
                                Application(
                                    name=charm.name,
                                    charm=charm,
                                    config=self.random_test_config(charm),
                                )
                            }
                        ),
                        integrations=node.bundle.integrations,
                        platform=node.bundle.platform,
                        arch=node.bundle.arch,
                    ),
                    balance=node.balance,
                )
                for charm in child_charms
            }
        )

    @staticmethod
    def random_test_config(charm) -> CharmConfig:
        # If there are not test configs defined return empty
        if len(charm.test_configs) == 0:
            return CharmConfig()

        # Pick a random config
        # This function is not secure in cryptography, but should be fine to use here
        return random.choice(charm.test_configs)  # nosec B311

    def _get_injected_charm_fulfillments(self, bundle: Bundle) -> set[tuple[ApplicationEndpoint, str]]:
        """
        Find injected charm fulfillments for endpoints that have reached their limits.
        
        When an endpoint has reached its connection limit but has an inject_charm override,
        this method identifies which unfulfilled endpoints could be satisfied by injecting
        the specified charm.
        
        Returns:
            Set of (unfulfilled_endpoint, injected_charm_name) tuples
        """
        injected_fulfillments = set()
        connection_counts = bundle.endpoint_connection_counts
        
        for application_endpoint, charm_endpoint in bundle.application_endpoints.items():
            # Check if endpoint has reached its limit
            if (charm_endpoint.limit is not None and 
                connection_counts.get(application_endpoint, 0) >= charm_endpoint.limit):
                
                # Get charm metadata overrides to check for inject_charm
                application = next(app for app in bundle.applications if app.name == application_endpoint.application)
                metadata_overrides = self.charmhub_client.overrides_client.get_charm_metadata_overrides(application.charm.name)
                
                # Get the appropriate overrides map based on endpoint type
                overrides_map = {}
                if charm_endpoint.type == ENDPOINT_PEERS:
                    overrides_map = metadata_overrides.peers
                elif charm_endpoint.type == ENDPOINT_REQUIRES:
                    overrides_map = metadata_overrides.requires
                elif charm_endpoint.type == ENDPOINT_PROVIDES:
                    overrides_map = metadata_overrides.provides
                
                # Check if this endpoint has an inject_charm override
                if (application_endpoint.endpoint in overrides_map and 
                    overrides_map[application_endpoint.endpoint].inject_charm is not None):
                    
                    inject_charm_name = overrides_map[application_endpoint.endpoint].inject_charm
                    self.logger.debug(
                        f"Found inject_charm override for {application_endpoint}: {inject_charm_name} "
                        f"(limit reached: {connection_counts.get(application_endpoint, 0)}/{charm_endpoint.limit})"
                    )
                    
                    # Find unfulfilled endpoints that could be fulfilled by the injected charm
                    for unfulfilled_endpoint in bundle.unfulfilled_endpoints:
                        unfulfilled_charm_endpoint = bundle.application_endpoints[unfulfilled_endpoint]
                        
                        # Injected charm is assumed to be compatible with the same interface
                        if unfulfilled_charm_endpoint.interface == charm_endpoint.interface:
                            self.logger.debug(
                                f"Adding injected charm {inject_charm_name} as possible fulfillment for {unfulfilled_endpoint}"
                            )
                            injected_fulfillments.add((unfulfilled_endpoint, inject_charm_name))
        
        return injected_fulfillments
