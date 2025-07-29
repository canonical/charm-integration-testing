# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

from pydantic.dataclasses import dataclass

from bundle_builder import (
    ENDPOINT_PROVIDES,
    ENDPOINT_REQUIRES,
    Application,
    ApplicationEndpoint,
    Bundle,
    BundleBuilder,
    Charm,
    CharmEndpoint,
    CharmhubClient,
    Integration,
    Node,
)


@dataclass(frozen=True)
class NeighborEndpointMap:
    endpoint: CharmEndpoint
    base_bundle: Bundle
    minimal_bundle: Bundle
    minimal_bundle_nodes: tuple[Node, ...] | None = None


@dataclass(frozen=True)
class NeighborMap:
    neighbor: Charm
    endpoints: frozenset[NeighborEndpointMap]


@dataclass(frozen=True)
class EndpointMap:
    endpoint: CharmEndpoint
    neighbors: frozenset[NeighborMap]


@dataclass(frozen=True)
class VersionMap:
    version: Charm
    base_bundle: Bundle
    minimal_bundle: Bundle
    endpoints: frozenset[EndpointMap]
    minimal_bundle_nodes: tuple[Node, ...] | None = None


@dataclass(frozen=True)
class ArchMap:
    arch: str
    versions: frozenset[VersionMap]


@dataclass(frozen=True)
class PlatformMap:
    platform: str
    arches: frozenset[ArchMap]


@dataclass(frozen=True)
class CharmMap:
    name: str
    platforms: frozenset[PlatformMap]


@dataclass(frozen=True)
class CharmhubMap:
    charms: frozenset[CharmMap]


class CharmhubMapper:
    charmhub_client: CharmhubClient
    bundle_builder: BundleBuilder
    logger: logging.Logger
    map_only_base_bundle: bool
    with_node_tree: bool

    def __init__(
        self,
        charmhub_client: CharmhubClient,
        bundle_builder: BundleBuilder,
        logger: logging.Logger,
        map_only_base_bundle: bool = False,
        with_node_tree: bool = False,
    ):
        self.charmhub_client = charmhub_client
        self.bundle_builder = bundle_builder
        self.logger = logger
        self.map_only_base_bundle = map_only_base_bundle
        self.with_node_tree = with_node_tree

    def map_charmhub(self, platforms: set[str], arches: set[str], charms: set[str] | None = None) -> CharmhubMap:
        # Fetch charms
        if charms is None:
            charms = self.charmhub_client.find_charms()

        # Map charms
        return CharmhubMap(
            charms=frozenset({self.map_charm(charm, platforms, arches) for charm in charms}),
        )

    def map_charm(self, charm: str, platforms: set[str], arches: set[str]) -> CharmMap:
        return CharmMap(
            name=charm,
            platforms=frozenset({self.map_platform(charm, platform, arches) for platform in platforms}),
        )

    def map_platform(self, charm: str, platform: str, arches: set[str]) -> PlatformMap:
        # Ensure charm supports platform
        if charm not in self.charmhub_client.find_charms(platform=platform):
            return PlatformMap(
                platform=platform,
                arches=frozenset({}),
            )

        # Return platform map
        return PlatformMap(
            platform=platform,
            arches=frozenset({self.map_arch(charm, platform, arch) for arch in arches}),
        )

    def map_arch(self, charm: str, platform: str, arch: str) -> ArchMap:
        # Find versions
        # Only check default release for arch
        versions = {self.charmhub_client.charm_from_store(charm_name=charm, ubuntu_arch=arch)}

        # Return arch map
        return ArchMap(
            arch=arch,
            versions=frozenset({self.map_version(charm, platform, arch, version) for version in versions}),
        )

    def map_version(self, charm: str, platform: str, arch: str, version: Charm) -> VersionMap:
        # Get bundles
        base_bundle = Bundle(
            applications=frozenset({Application(name="target", charm=version)}),
            integrations=frozenset({}),
            platform=platform,
            arch=arch,
        )
        minimal_bundle, minimal_bundle_nodes = self.bundle_builder.build(base_bundle)

        endpoints = {}
        if not self.map_only_base_bundle:
            endpoints = {
                self.map_endpoint(charm, platform, arch, version, endpoint)
                for endpoint in version.endpoints
                if endpoint.type in {ENDPOINT_PROVIDES, ENDPOINT_REQUIRES}
            }

        # Return version map
        return VersionMap(
            version=version,
            base_bundle=base_bundle,
            minimal_bundle=minimal_bundle,
            minimal_bundle_nodes=tuple(minimal_bundle_nodes) if self.with_node_tree else None,
            endpoints=frozenset(endpoints),
        )

    def map_endpoint(
        self, charm: str, platform: str, arch: str, version: Charm, endpoint: CharmEndpoint
    ) -> EndpointMap:
        # Find all integrating charms for this endpoint
        neighbor_names = set()
        if endpoint.type == ENDPOINT_PROVIDES:
            neighbor_names = self.charmhub_client.find_charms(requires=endpoint.interface, platform=platform)
        elif endpoint.type == ENDPOINT_REQUIRES:
            neighbor_names = self.charmhub_client.find_charms(provides=endpoint.interface, platform=platform)
        else:
            return EndpointMap(
                endpoint=endpoint,
                neighbors=frozenset({}),
            )

        # Find all neighbor versions
        neighbors = {
            self.charmhub_client.charm_from_store(charm_name=charm_name, ubuntu_arch=arch)
            for charm_name in neighbor_names
        }

        # Return endpoint map
        return EndpointMap(
            endpoint=endpoint,
            neighbors=frozenset(
                {self.map_neighbor(charm, platform, arch, version, endpoint, neighbor) for neighbor in neighbors}
            ),
        )

    def map_neighbor(
        self, charm: str, platform: str, arch: str, version: Charm, endpoint: CharmEndpoint, neighbor: Charm
    ) -> NeighborMap:
        return NeighborMap(
            neighbor=neighbor,
            endpoints=frozenset(
                {
                    self.map_neighbor_endpoint(charm, platform, arch, version, endpoint, neighbor, neighbor_endpoint)
                    for neighbor_endpoint in neighbor.endpoints
                    if (neighbor_endpoint.interface == endpoint.interface)
                    and (
                        (neighbor_endpoint.type == ENDPOINT_PROVIDES and endpoint.type == ENDPOINT_REQUIRES)
                        or (neighbor_endpoint.type == ENDPOINT_REQUIRES and endpoint.type == ENDPOINT_PROVIDES)
                    )
                }
            ),
        )

    def map_neighbor_endpoint(
        self,
        charm: str,
        platform: str,
        arch: str,
        version: Charm,
        endpoint: CharmEndpoint,
        neighbor: Charm,
        neighbor_endpoint: CharmEndpoint,
    ) -> NeighborEndpointMap:
        # Get bundles
        base_bundle = Bundle(
            applications=frozenset(
                {
                    Application(name="target", charm=version),
                    Application(name="neighbor", charm=neighbor),
                }
            ),
            integrations=frozenset(
                {
                    Integration(
                        {
                            ApplicationEndpoint(application="target", endpoint=endpoint.name),
                            ApplicationEndpoint(application="neighbor", endpoint=neighbor_endpoint.name),
                        }
                    )
                }
            ),
            platform=platform,
            arch=arch,
        )
        minimal_bundle, minimal_bundle_nodes = self.bundle_builder.build(base_bundle)

        # Return neighbor map
        return NeighborEndpointMap(
            endpoint=neighbor_endpoint,
            base_bundle=base_bundle,
            minimal_bundle=minimal_bundle,
            minimal_bundle_nodes=tuple(minimal_bundle_nodes) if self.with_node_tree else None,
        )
