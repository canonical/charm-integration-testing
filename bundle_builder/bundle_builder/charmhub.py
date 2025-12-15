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
import logging
from functools import cache

from .charm import (
    ENDPOINT_PEERS,
    ENDPOINT_PROVIDES,
    ENDPOINT_REQUIRES,
    Charm,
    CharmEndpoint,
    CharmEndpointOptionality,
    CharmTestConfig,
)
from .charmhub_http import (
    CharmhubBase,
    CharmhubHttpClient,
    CharmMetadata,
    CharmReleaseNotFoundException,
    FindResponse,
    RefreshAction,
    RefreshResponse,
)
from .overrides import CharmMetadataOverride, OverridesClient


class CharmhubClient:
    http_client: CharmhubHttpClient
    logger: logging.Logger
    overrides_client: OverridesClient

    def __init__(
        self,
        http_client: CharmhubHttpClient | None = None,
        logger: logging.Logger = logging.getLogger(__name__),
        overrides_client: OverridesClient | None = None,
    ) -> None:
        self.http_client = http_client if http_client is not None else CharmhubHttpClient(logger=logger)
        self.logger = logger
        self.overrides_client = overrides_client if overrides_client is not None else OverridesClient()

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
        response = set(self.http_client.find(provides=provides, requires=requires))

        # Add charms from listing overrides
        response |= self._find_charms_get_listing_overrides(provides=provides, requires=requires)

        # Add platform overrides
        response = self._find_charms_add_platform_overrides(response)

        response = self._find_charms_add_deployable_on_overrides(response)

        # Filter response by platform
        if platform is not None:
            response = {charm for charm in response if platform in charm.result.deployable_on}

        # Return charms
        return frozenset({charm.name for charm in response})

    def _find_charms_get_listing_overrides(
        self, provides: str | None = None, requires: str | None = None
    ) -> set[FindResponse]:
        # Get charm info for each listing overridden charm
        charms: set[FindResponse] = set()
        for charm in self.overrides_client.get_charm_listing_overrides():
            # Get charm info
            charm_info = self.http_client.info(charm)

            # Filter results
            if provides is not None and provides not in {
                endpoint.interface for endpoint in charm_info.default_release.revision.metadata.provides.values()
            }:
                continue
            if requires is not None and requires not in {
                endpoint.interface for endpoint in charm_info.default_release.revision.metadata.requires.values()
            }:
                continue

            # Add find response
            charms.add(
                FindResponse(
                    name=charm,
                    result=FindResponse.Result(deployable_on=charm_info.result.deployable_on),
                )
            )

        return charms

    def _find_charms_add_platform_overrides(self, response: set[FindResponse]) -> set[FindResponse]:
        return {
            dataclasses.replace(
                charm,
                result=dataclasses.replace(
                    charm.result,
                    deployable_on=frozenset(
                        charm.result.deployable_on | self.overrides_client.get_charm_platform_overrides(charm.name)
                    ),
                ),
            )
            for charm in response
        }

    def _find_charms_add_deployable_on_overrides(self, response: set[FindResponse]) -> set[FindResponse]:
        # If response[n].charm.result.deployable_on is empty, then it is deployable on machine environments by default.
        return {
            dataclasses.replace(
                charm,
                result=dataclasses.replace(
                    charm.result,
                    deployable_on=frozenset(["machine"])
                    if len(charm.result.deployable_on) == 0
                    else charm.result.deployable_on,
                ),
            )
            for charm in response
        }

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
            bases = refresh_info.charm.bases if refresh_info.charm is not None else []
            for base in bases or []:
                if base.name == "ubuntu" and base.architecture == ubuntu_arch:
                    ubuntu_version = base.channel
                    break
            else:
                # No valid ubuntu version found
                raise CharmReleaseNotFoundException(
                    f"Charm {charm_name} revision {charm_revision} does not appear to support arch {ubuntu_arch}"
                )

        # Find suitable channel (must support base)
        default_refresh_info = self._default_refresh_info(
            charm_name,
            CharmhubBase(
                channel=ubuntu_version,
                architecture=ubuntu_arch,
            ),
        )

        # Return Charm from refresh info
        if default_refresh_info.effective_channel is None:
            raise CharmReleaseNotFoundException(
                f"Failed to find suitable channel for charm {charm_name} revision {charm_revision} with ubuntu version {ubuntu_version} and arch {ubuntu_arch}"
            )

        return Charm(
            name=charm_name,
            channel=default_refresh_info.effective_channel,
            revision=charm_revision,
            ubuntu_version=ubuntu_version,
            ubuntu_arch=ubuntu_arch,
            endpoints=self._all_charm_endpoints(refresh_info),
            test_configs=self._charm_test_configs(charm_name),
            priority=self._get_charm_priority(charm_name),
        )

    def _charm_from_store_by_channel(
        self,
        charm_name: str,
        ubuntu_arch: str,
        charm_channel: str,
        ubuntu_version: str | None = None,
    ) -> Charm:
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

        if refresh_info.charm is None:
            raise ValueError(
                f"Refresh info for charm {charm_name} in channel {charm_channel} returned no charm and no error"
            )
        if refresh_info.charm.revision is None:
            raise ValueError(f"Refresh info for charm {charm_name} in channel {charm_channel} returned no revision")

        return Charm(
            name=charm_name,
            channel=charm_channel,
            revision=refresh_info.charm.revision,
            ubuntu_version=ubuntu_version,
            ubuntu_arch=ubuntu_arch,
            endpoints=self._all_charm_endpoints(refresh_info),
            test_configs=self._charm_test_configs(charm_name),
            priority=self._get_charm_priority(charm_name),
        )

    def _charm_from_store_default(
        self,
        charm_name: str,
        ubuntu_arch: str,
        ubuntu_version: str | None = None,
    ) -> Charm:
        # Get default ubuntu version if not provided
        if not ubuntu_version:
            ubuntu_version = self._default_ubuntu_version(charm_name, ubuntu_arch)

        # Get default refresh info
        refresh_info = self._default_refresh_info(
            charm_name,
            CharmhubBase(
                channel=ubuntu_version,
                architecture=ubuntu_arch,
            ),
        )

        # Return Charm
        if refresh_info.effective_channel is None:
            raise CharmReleaseNotFoundException(
                f"Failed to find suitable channel for charm {charm_name} with ubuntu version {ubuntu_version} and arch {ubuntu_arch}"
            )
        if refresh_info.charm is None:
            raise ValueError(f"Refresh info for charm {charm_name} returned no charm and no error")
        if refresh_info.charm.revision is None:
            raise ValueError(f"Refresh info for charm {charm_name} returned no revision")
        return Charm(
            name=charm_name,
            channel=refresh_info.effective_channel,
            revision=refresh_info.charm.revision,
            ubuntu_version=ubuntu_version,
            ubuntu_arch=ubuntu_arch,
            endpoints=self._all_charm_endpoints(refresh_info),
            test_configs=self._charm_test_configs(charm_name),
            priority=self._get_charm_priority(charm_name),
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

        # Check the error code to get the bases
        expected_error_codes = ["invalid-charm-base", "revision-not-found"]
        if refresh_info.error is None or refresh_info.error.code not in expected_error_codes:
            raise CharmReleaseNotFoundException(f"Failed to find default bases for charm {charm_name}")
        else:
            if refresh_info.error.extra is None:
                raise CharmReleaseNotFoundException(f"No extra information for default bases of {charm_name}")
            if refresh_info.error.code == "invalid-charm-base":
                bases = refresh_info.error.extra.default_bases
            elif refresh_info.error.code == "revision-not-found":
                bases = [release.base for release in refresh_info.error.extra.releases]

        # Ensure a base was found
        if len(bases) == 0:
            raise CharmReleaseNotFoundException(f"No default bases found for {charm_name} in arch {ubuntu_arch}")

        # Pick the first base (like Juju)
        return bases[0].channel

    def _default_refresh_info(self, charm_name: str, base: CharmhubBase) -> RefreshResponse:
        # Get refresh info for base
        refresh_info = self.http_client.refresh(RefreshAction(charm_name=charm_name, base=base))
        if refresh_info.error is None:
            return refresh_info

        # If error check extra releases for base
        if refresh_info.error and refresh_info.error.code == "revision-not-found":
            # Gather channels with matching base
            if refresh_info.error.extra is None:
                channels = set()
            else:
                channels = {release.channel for release in refresh_info.error.extra.releases if release.base == base}

            # Try channels with default track as well
            channels |= {f"latest/{channel}" for channel in channels if "/" not in channel}

            # Try calling refresh with each channel
            for channel in channels:
                refresh_info = self.http_client.refresh(
                    RefreshAction(charm_name=charm_name, base=base, charm_channel=channel)
                )
                if refresh_info.error is None:
                    break

        # Check refresh info for error
        if refresh_info.error:
            raise CharmReleaseNotFoundException(
                f"Failed to find default release for charm {charm_name} with base {base}: {refresh_info.error.message}"
            )

        return refresh_info

    def _all_charm_endpoints(self, refresh_info: RefreshResponse) -> frozenset[CharmEndpoint]:
        metadata = refresh_info.charm.metadata if refresh_info.charm is not None else CharmMetadata()

        # Get edge refresh info if any requires or provides endpoints don't have optional flag
        edge_metadata = CharmMetadata()
        if any(
            endpoint.optional is None for endpoint in set(metadata.requires.values()) | set(metadata.provides.values())
        ):
            edge_refresh_info = self.http_client.refresh(
                RefreshAction(
                    charm_name=refresh_info.name,
                    charm_channel="edge",
                    base=next(iter(refresh_info.charm.bases))
                    if refresh_info.charm is not None and refresh_info.charm.bases is not None
                    else None,
                ),
            )
            if edge_refresh_info.error is None and edge_refresh_info.charm is not None:
                edge_metadata = edge_refresh_info.charm.metadata

        # Get endpoint optionality overrides
        metadata_overrides = CharmMetadataOverride()
        if self.overrides_client:
            metadata_overrides = self.overrides_client.get_charm_metadata_overrides(refresh_info.name)

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
                    # mypy doesn't understand optionality will not be None in this case
                    if optionality is None:
                        raise RuntimeError("Unexpected None optionality after check")
                elif endpoint.optional is not None:
                    optionality = CharmEndpointOptionality.from_bool(endpoint.optional)
                elif endpoint_name in edge_endpoint_map and edge_endpoint_map[endpoint_name].optional is not None:
                    optionality = CharmEndpointOptionality.from_bool(edge_endpoint_map[endpoint_name].optional or False)
                elif endpoint_type in {ENDPOINT_PROVIDES, ENDPOINT_REQUIRES}:
                    optionality = CharmEndpointOptionality.from_bool(False)
                else:
                    optionality = CharmEndpointOptionality.from_bool(True)

                # Determine endpoint limit from overrides
                if endpoint_name in metadata_overrides_map and metadata_overrides_map[endpoint_name].limit is not None:
                    limit = metadata_overrides_map[endpoint_name].limit
                elif endpoint.limit is not None:
                    limit = endpoint.limit
                elif endpoint_name in edge_endpoint_map and edge_endpoint_map[endpoint_name].limit is not None:
                    limit = edge_endpoint_map[endpoint_name].limit
                else:
                    limit = None

                # Add endpoint
                endpoints.add(
                    CharmEndpoint(
                        type=endpoint_type,
                        name=endpoint_name,
                        interface=endpoint.interface,
                        optionality=optionality,
                        limit=limit,
                    )
                )

        return frozenset(endpoints)

    def _charm_test_configs(self, charm: str) -> tuple[CharmTestConfig, ...]:
        # Get test configs from overrides client
        return tuple(self.overrides_client.get_charm_test_configs(charm))

    def _get_charm_priority(self, charm_name: str) -> float:
        return self.overrides_client.get_charm_priorities_mapping().get(charm_name, 1.0)
