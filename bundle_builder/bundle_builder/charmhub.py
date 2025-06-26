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
from functools import cache

from .charm import ENDPOINT_PEERS, ENDPOINT_PROVIDES, ENDPOINT_REQUIRES, Charm, CharmEndpoint, CharmEndpointOptionality
from .charmhub_http import (
    CharmhubBase,
    CharmhubHttpClient,
    CharmMetadata,
    CharmReleaseNotFoundException,
    RefreshAction,
    RefreshResponse,
)
from .overrides import CharmMetadataOverride, OverridesClient


class CharmhubClient:
    http_client: CharmhubHttpClient
    logger: logging.Logger
    overrides_client: OverridesClient | None

    def __init__(
        self,
        http_client: CharmhubHttpClient | None = None,
        logger=logging.getLogger(__name__),
        overrides_client: OverridesClient | None = None,
    ):
        self.http_client = http_client or CharmhubHttpClient(logger=logger)
        self.logger = logger
        self.overrides_client = overrides_client

    @cache
    def charm_from_store(
        self,
        charm_name: str,
        ubuntu_arch: str,
        charm_channel: str | None = None,
        charm_revision: int | None = None,
        ubuntu_version: str | None = None,
    ) -> Charm:
        # Figure out how to look up charm information
        if charm_channel and charm_revision:
            self.logger.error(
                "Both charm_channel and charm_revision passed to charm initialization. Using charm revision"
            )
            return self._charm_from_store_by_revision(
                charm_name=charm_name,
                ubuntu_arch=ubuntu_arch,
                charm_revision=charm_revision,
                ubuntu_version=ubuntu_version,
            )
        elif charm_revision:
            return self._charm_from_store_by_revision(
                charm_name=charm_name,
                ubuntu_arch=ubuntu_arch,
                charm_revision=charm_revision,
                ubuntu_version=ubuntu_version,
            )
        elif charm_channel:
            return self._charm_from_store_by_channel(
                charm_name=charm_name,
                ubuntu_arch=ubuntu_arch,
                charm_channel=charm_channel,
                ubuntu_version=ubuntu_version,
            )
        else:
            return self._charm_from_store_default(
                charm_name=charm_name,
                ubuntu_arch=ubuntu_arch,
                ubuntu_version=ubuntu_version,
            )

    @cache
    def find_charms(
        self, provides: str | None = None, requires: str | None = None, platform: str | None = None
    ) -> frozenset[str]:
        # Call find API
        response = self.http_client.find(provides=provides, requires=requires)

        # Return charms
        return frozenset(
            {charm.name for charm in response if platform is None or platform in charm.result.deployable_on}
        )

    def _charm_from_store_by_revision(
        self,
        charm_name: str,
        ubuntu_arch: str,
        charm_revision: int,
        ubuntu_version: str | None = None,
    ) -> Charm:
        # Get refresh info for revision
        refresh_info = self.http_client.refresh(
            RefreshAction(
                charm_name=charm_name,
                charm_revision=charm_revision,
                always_include_base=True,
            )
        )
        if refresh_info.error is not None:
            raise CharmReleaseNotFoundException(
                f"Failed to find charm {charm_name} for revision {charm_revision}: {refresh_info.error.message}"
            )

        # Find suitable ubuntu version for revision
        if not ubuntu_version:
            # Return first ubuntu version with matching base
            for base in refresh_info.charm.bases:
                if base.name == "ubuntu" and base.architecture == ubuntu_arch:
                    ubuntu_version = base.channel
                    break
            else:
                # No valid ubuntu version found
                raise CharmReleaseNotFoundException(
                    f"Charm {charm_name} revision {charm_revision} does not appear to support arch {ubuntu_arch}"
                )

        # Find suitable channel (must support base)
        charm_channel = self._suitable_charm_channel(
            charm_name,
            CharmhubBase(
                channel=ubuntu_version,
                architecture=ubuntu_arch,
            ),
        )

        # Return Charm from refresh info
        return Charm(
            name=charm_name,
            channel=charm_channel,
            revision=charm_revision,
            ubuntu_version=ubuntu_version,
            ubuntu_arch=ubuntu_arch,
            endpoints=self._all_charm_endpoints(refresh_info),
        )

    def _charm_from_store_by_channel(
        self,
        charm_name: str,
        ubuntu_arch: str,
        charm_channel: str,
        ubuntu_version: str | None = None,
    ):
        # Get default ubuntu version if not given
        if not ubuntu_version:
            ubuntu_version = self._default_ubuntu_version(charm_name, ubuntu_arch, charm_channel=charm_channel)

        # Call refresh with channel and base
        refresh_info = self.http_client.refresh(
            RefreshAction(
                charm_name=charm_name,
                charm_channel=charm_channel,
                base=CharmhubBase(
                    channel=ubuntu_version,
                    architecture=ubuntu_arch,
                ),
            )
        )
        if refresh_info.error is not None:
            raise CharmReleaseNotFoundException(
                f"Failed to find release for charm {charm_name} in channel {charm_channel} with ubuntu version {ubuntu_version}: {refresh_info.error.message}"
            )

        # Return Charm
        return Charm(
            name=charm_name,
            channel=charm_channel,
            revision=refresh_info.charm.revision,
            ubuntu_version=ubuntu_version,
            ubuntu_arch=ubuntu_arch,
            endpoints=self._all_charm_endpoints(refresh_info),
        )

    def _charm_from_store_default(
        self,
        charm_name: str,
        ubuntu_arch: str,
        ubuntu_version: str | None = None,
    ):
        # Get default ubuntu version if not given
        if not ubuntu_version:
            ubuntu_version = self._default_ubuntu_version(charm_name, ubuntu_arch)

        # Call refresh with base
        refresh_info = self.http_client.refresh(
            RefreshAction(
                charm_name=charm_name,
                base=CharmhubBase(
                    channel=ubuntu_version,
                    architecture=ubuntu_arch,
                ),
            )
        )
        if refresh_info.error is not None:
            raise CharmReleaseNotFoundException(
                f"Failed to find default release for charm {charm_name} with ubuntu version {ubuntu_version}: {refresh_info.error.message}"
            )

        # Return Charm
        return Charm(
            name=charm_name,
            channel=refresh_info.effective_channel,
            revision=refresh_info.charm.revision,
            ubuntu_version=ubuntu_version,
            ubuntu_arch=ubuntu_arch,
            endpoints=self._all_charm_endpoints(refresh_info),
        )

    def _default_ubuntu_version(self, charm_name: str, ubuntu_arch: str, charm_channel: str | None = None) -> str:
        # Juju passes "NA" to get the secret "default-bases" error field
        refresh_info = self.http_client.refresh(
            RefreshAction(
                charm_name=charm_name,
                charm_channel=charm_channel,
                base=CharmhubBase(
                    name="NA",
                    channel="NA",
                    architecture=ubuntu_arch,
                ),
            )
        )
        if refresh_info.error.code != "invalid-charm-base":
            raise CharmReleaseNotFoundException(f"Failed to find default bases for charm {charm_name}")

        # Get default bases field
        default_bases = refresh_info.error.extra.default_bases

        # Ensure a base was found
        if len(default_bases) == 0:
            raise CharmReleaseNotFoundException(f"No default bases found for {charm_name} in arch {ubuntu_arch}")

        # Pick the first base (like Juju)
        return default_bases[0].channel

    def _suitable_charm_channel(self, charm_name: str, base: CharmhubBase) -> str:
        # Get refresh info for base
        refresh_info = self.http_client.refresh(RefreshAction(charm_name=charm_name, base=base))
        if refresh_info.error is None:
            return refresh_info.effective_channel

        # Check extra releases for base
        if refresh_info.error.code == "revision-not-found":
            for release in refresh_info.error.extra.releases:
                if release.base == base:
                    return release.channel

        # No suitable channel found
        raise CharmReleaseNotFoundException(
            f"Failed to find default release for charm {charm_name}: {refresh_info.error.message}"
        )

    def _all_charm_endpoints(self, refresh_info: RefreshResponse):
        metadata = refresh_info.charm.metadata

        # Get edge refresh info if any required endpoints don't have optional flag
        edge_metadata = CharmMetadata()
        if any(endpoint.optional is None for endpoint in metadata.requires.values()):
            edge_refresh_info = self.http_client.refresh(
                RefreshAction(
                    charm_name=refresh_info.name,
                    charm_channel="edge",
                    base=next(iter(refresh_info.charm.bases)),
                ),
            )
            if edge_refresh_info.error is None:
                edge_metadata = edge_refresh_info.charm.metadata

        # Get endpoint optionality overrides
        metadata_overrides = CharmMetadataOverride()
        if self.overrides_client:
            metadata_overrides = self.overrides_client.get_charm_overrides(refresh_info.name)

        # Map endpoints
        endpoints = set()
        for endpoint_type, endpoint_map, edge_endpoint_map, metadata_overrides_map in (
            (ENDPOINT_PEERS, metadata.peers, edge_metadata.peers, metadata_overrides.peers),
            (ENDPOINT_REQUIRES, metadata.requires, edge_metadata.requires, metadata_overrides.requires),
            (ENDPOINT_PROVIDES, metadata.provides, edge_metadata.provides, metadata_overrides.provides),
        ):
            for endpoint_name, endpoint in endpoint_map.items():
                # Determine endpoint optionality
                if (
                    endpoint_name in metadata_overrides_map
                    and metadata_overrides_map[endpoint_name].optionality is not None
                ):
                    optionality = metadata_overrides_map[endpoint_name].optionality
                elif endpoint.optional is not None:
                    optionality = CharmEndpointOptionality.from_bool(endpoint.optional)
                elif endpoint_name in edge_endpoint_map and edge_endpoint_map[endpoint_name].optional is not None:
                    optionality = CharmEndpointOptionality.from_bool(edge_endpoint_map[endpoint_name].optional)
                else:
                    optionality = CharmEndpointOptionality.from_bool(False)

                # Add endpoint
                endpoints.add(
                    CharmEndpoint(
                        type=endpoint_type,
                        name=endpoint_name,
                        interface=endpoint.interface,
                        optionality=optionality,
                    )
                )

        return frozenset(endpoints)
