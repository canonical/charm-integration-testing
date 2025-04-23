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
from dataclasses import dataclass
from functools import cached_property

from .bundle import Application, ApplicationEndpoint, Bundle
from .charmhub import CharmhubClient


@dataclass(frozen=True)
class Node:
    bundle: Bundle
    application_endpoint_to_possible_charm: frozenset[tuple[ApplicationEndpoint, str]]
    balance: float

    @cached_property
    def score(self) -> float:
        # balance changes the weight prioritizing number of applications over unfulfilled interfaces
        # it is expected to be between 0 and 1, where 1 prioritizes the smallest bundle
        return self.balance * len(self.bundle.applications) + (1.0 - self.balance) * len(
            self.bundle.unfulfilled_interfaces
        )

    @cached_property
    def fingerprint(self) -> frozenset[str]:
        return self.bundle.charms

    @cached_property
    def fulfillable_interfaces(self) -> frozenset[str]:
        return frozenset(
            {
                self.bundle.application_endpoints[application_endpoint].interface
                for application_endpoint, _ in self.application_endpoint_to_possible_charm
            }
        )

    @cached_property
    def child_charms(self) -> frozenset[str]:
        return frozenset({charm_name for _, charm_name in self.application_endpoint_to_possible_charm})

    @cached_property
    def stats(self) -> str:
        return f"{len(self.bundle.applications)} applications ({len(self.bundle.unfulfilled_interfaces)} unfulfilled and {len(self.fulfillable_interfaces)} fulfillable interfaces)"

    def __lt__(self, other):
        return self.score < other.score


class BundleBuilder:
    charmhub_client: CharmhubClient
    logger: logging.Logger
    max_nodes_visited: int = 100000
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

    # Return a new node, including the possible child charms
    def new_node(self, bundle: Bundle, balance: float = 1.0) -> Node:
        # Ensure all possible integrations are fulfilled by the bundle
        bundle = bundle.add_missing_integrations()

        # Get all possible ways to fulfill unfulfilled application endpoints with charms
        # Note that we explicitly remove the bundle charms as we cannot use a charm in the bundle to fulfill an unfulfillable interface
        # An example is grafana-agent-k8s provides and requires `tracing`, and is the only charm in Charmhub to use `tracing`
        application_endpoint_to_possible_charm = {
            (application_endpoint, charm_name)
            for application_endpoint in bundle.unfulfilled_endpoints
            for charm_name in (
                self.charmhub_client.find_charms(
                    provides=bundle.application_endpoints[application_endpoint].interface, platform=bundle.platform
                )
                - bundle.charms
            )
        }

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
        child_charm = {
            self.charmhub_client.charm_from_store(charm_name=charm_name, ubuntu_arch=node.bundle.arch)
            for charm_name in node.child_charms
        }

        # Create a child node for each child charm
        return frozenset(
            {
                self.new_node(
                    bundle=Bundle(
                        applications=frozenset(node.bundle.applications | {Application(name=charm.name, charm=charm)}),
                        integrations=node.bundle.integrations,
                        platform=node.bundle.platform,
                        arch=node.bundle.arch,
                    ),
                    balance=node.balance,
                )
                for charm in child_charm
            }
        )
