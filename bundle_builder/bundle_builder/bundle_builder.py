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
from dataclasses import dataclass
from functools import cached_property

from .bundle import Application, ApplicationEndpoint, Bundle
from .charmhub import CharmhubClient


@dataclass(frozen=True)
class Node:
    bundle: Bundle
    application_endpoint_to_possible_charm: frozenset[tuple[ApplicationEndpoint, str]]

    @cached_property
    def score(self) -> float:
        # Somewhat arbitrary weights
        # 0.6/0.4 split means adding 6 applications is equal to 4 unfulfilled interfaces
        # Basically adding one application shouldn't increase unfulfilled interfaces,
        # and lean towards smaller bundles
        return 0.6 * len(self.bundle.applications) + 0.4 * len(self.bundle.unfulfilled_interfaces)

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


class BundleBuilder:
    charmhub_client: CharmhubClient
    logger: logging.Logger

    def __init__(self, charmhub_client: CharmhubClient, logger=logging.getLogger(__name__)):
        self.charmhub_client = charmhub_client
        self.logger = logger

    # Build out the bundle, pulling in charms that fulfill non-optional hanging required integrations
    def build(self, base: Bundle) -> Bundle:
        # This follows a rough uniform cost algorithm
        queued_nodes = [self.new_node(base)]
        best_node = queued_nodes[0]
        visited_nodes = {best_node.fingerprint}
        self.logger.info(f"Starting with bundle: {best_node.stats}")
        while len(queued_nodes) > 0:
            # Get node with the best score
            node = min(queued_nodes, key=lambda node: node.score)
            queued_nodes.remove(node)
            self.logger.debug(f"Checking bundle: {node.stats}")

            # If there are no fulfillable interfaces quit now
            # We could exhaustively search the graph but that comes at the cost of compute
            if len(node.fulfillable_interfaces) == 0:
                self.logger.info("Bundle has no more fulfillable interfaces, stopping")
                best_node = node
                break

            # Add this node's children to the back of the queue, as long as not visited
            for child_node in self.child_nodes(node):
                if child_node.fingerprint not in visited_nodes:
                    visited_nodes.add(child_node.fingerprint)
                    queued_nodes.append(child_node)

        # Note unresolved endpoints
        for application_endpoint in best_node.bundle.unfulfilled_endpoints:
            self.logger.warning(f"Cannot resolve application endpoint: {application_endpoint}")

        # Return best node
        return best_node.bundle

    # Return a new node, including the possible child charms
    def new_node(self, bundle: Bundle) -> Node:
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
                    Bundle(
                        applications=frozenset(node.bundle.applications | {Application(name=charm.name, charm=charm)}),
                        integrations=node.bundle.integrations,
                        platform=node.bundle.platform,
                        arch=node.bundle.arch,
                    )
                )
                for charm in child_charm
            }
        )
