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

import requests
import yaml
from pydantic import Field, field_validator
from pydantic.dataclasses import dataclass
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .charm import ENDPOINT_PEERS, ENDPOINT_PROVIDES, ENDPOINT_REQUIRES, Charm, CharmEndpoint, CharmEndpointOptionality
from .overrides import CharmMetadataOverride, OverridesClient


class UnparsableCharmException(Exception):
    """Raised when the charm cannot be parsed."""

    pass


class NoCharmMetadataException(UnparsableCharmException):
    """Raised when there is no metadata-yaml exposed by the charmhub for this charm."""

    pass


class CharmReleaseNotFoundException(Exception):
    """Raised when the release for a charm cannot be deduced."""

    pass


@dataclass(frozen=True)
class CharmhubBase:
    architecture: str
    channel: str
    name: str = Field(default="ubuntu")


@dataclass(frozen=True)
class RefreshAction:
    charm_name: str
    charm_revision: int | None = None
    charm_channel: str | None = None
    base: CharmhubBase | None = None
    always_include_base: bool = False


@dataclass(frozen=True)
class CharmMetadata:
    @dataclass(frozen=True)
    class Endpoint:
        interface: str
        optional: bool | None = None

    peers: dict[str, Endpoint] = Field(default_factory=dict)
    requires: dict[str, Endpoint] = Field(default_factory=dict)
    provides: dict[str, Endpoint] = Field(default_factory=dict)


@dataclass(frozen=True)
class RefreshResponse:
    @dataclass(frozen=True)
    class Charm:
        bases: list[CharmhubBase] | None = None
        revision: int | None = None
        metadata: CharmMetadata | None = Field(default=None, alias="metadata-yaml")

        @field_validator("metadata", mode="before")
        @classmethod
        def parse_yaml(cls, metadata_yaml):
            return CharmMetadata(**yaml.safe_load(metadata_yaml))

    @dataclass(frozen=True)
    class Error:
        @dataclass(frozen=True)
        class Extra:
            @dataclass(frozen=True)
            class Release:
                base: CharmhubBase
                channel: str

            default_bases: list[CharmhubBase] = Field(default_factory=list, alias="default-bases")
            releases: list[Release] = Field(default_factory=list)

        message: str
        code: str
        extra: Extra | None = None

    name: str
    charm: Charm | None = None
    effective_channel: str | None = Field(default=None, alias="effective-channel")
    error: Error | None = None


CHARM_INFO_ENDPOINT = "https://api.charmhub.io/v2/charms/info/{charm_name}"
CHARM_REFRESH_ENDPOINT = "https://api.charmhub.io/v2/charms/refresh"
CHARM_STORE_JSON_ENDPOINT = "https://charmhub.io/store.json"
CHARM_FIND_ENDPOINT = "https://api.charmhub.io/v2/charms/find"


class CharmhubClient:
    session: requests.Session
    logger: logging.Logger
    overrides_client: OverridesClient | None

    def __init__(self, logger=logging.getLogger(__name__), overrides_client: OverridesClient | None = None):
        self.logger = logger
        self.overrides_client = overrides_client

        # Setup requests session with retries
        retry_strategy = Retry(
            total=10,
            status_forcelist=[429, 500, 502, 503, 504],
            backoff_factor=0.5,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session = requests.Session()
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

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
        response = self._call_find(provides=provides, requires=requires)

        # Parse response
        charms = {
            charm.get("name", None)
            for charm in response.get("results", [])
            if platform is None or platform in charm.get("result", {}).get("deployable-on", [])
        }

        # Return charms
        return frozenset({charm for charm in charms if charm is not None})

    def _charm_from_store_by_revision(
        self,
        charm_name: str,
        ubuntu_arch: str,
        charm_revision: int,
        ubuntu_version: str | None = None,
    ) -> Charm:
        # Get refresh info for revision
        refresh_info = self._call_refresh(
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
        refresh_info = self._call_refresh(
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
        refresh_info = self._call_refresh(
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
        refresh_info = self._call_refresh(
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
        refresh_info = self._call_refresh(RefreshAction(charm_name=charm_name, base=base))
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
            edge_refresh_info = self._call_refresh(
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
                elif endpoint_type == ENDPOINT_REQUIRES:
                    optionality = CharmEndpointOptionality.from_bool(False)
                else:
                    optionality = CharmEndpointOptionality.from_bool(True)

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

    @cache
    def _call_store_json(self, provides: str | None = None) -> dict:
        self.logger.debug(f"Calling store json with provides {provides}")

        # Formulate request
        request_url = CHARM_STORE_JSON_ENDPOINT
        request_headers = {"Content-Type": "application/json"}
        request_params = {"size": 300, "type": "charm", **({"provides": provides if provides is not None else {}})}

        # Execute request
        response = self.session.get(url=request_url, params=request_params, headers=request_headers, timeout=180)
        response.raise_for_status()
        return response.json()

    @cache
    def _call_find(self, provides: str | None = None, requires: str | None = None) -> dict:
        self.logger.debug(f"Calling find with provides {provides} and requires {requires}")

        # Formulate request
        request_url = CHARM_FIND_ENDPOINT
        request_headers = {"Content-Type": "application/json"}
        request_params = {
            "q": "",
            "type": "charm",
            "fields": "result.deployable-on",
            **({"provides": provides if provides is not None else {}}),
            **({"requires": requires if requires is not None else {}}),
        }

        # Execute request
        response = self.session.get(url=request_url, params=request_params, headers=request_headers, timeout=180)
        response.raise_for_status()
        return response.json()

    @cache
    def _call_refresh(self, action: RefreshAction) -> RefreshResponse:
        print_properties = ", ".join(
            sorted(
                f"{key}: {value}"
                for key, value in {
                    "revision": action.charm_revision,
                    "channel": action.charm_channel,
                    "base": f"{action.base.name}:{action.base.channel} {action.base.architecture}"
                    if action.base
                    else None,
                }.items()
                if value is not None
            )
        )
        self.logger.debug(f"Calling refresh for charm {action.charm_name} ({print_properties})")

        # Formulate request
        request_url = CHARM_REFRESH_ENDPOINT
        request_headers = {"Content-Type": "application/json"}
        action_dict = {"name": action.charm_name}
        if action.charm_revision is not None:
            action_dict["revision"] = action.charm_revision
        if action.charm_channel is not None:
            action_dict["channel"] = action.charm_channel
        if action.base is not None:
            action_dict["base"] = {
                "name": action.base.name,
                "channel": action.base.channel,
                "architecture": action.base.architecture,
            }
        elif action.always_include_base:
            action_dict["base"] = None
        request_body = {
            "context": [],
            "actions": [{"action": "install", "instance-key": "1", **action_dict}],
            "fields": ["bases", "metadata-yaml", "revision"],
        }

        # Execute request
        response = self.session.post(url=request_url, json=request_body, headers=request_headers, timeout=180)
        response.raise_for_status()
        response_json = response.json()
        return RefreshResponse(**next(iter(response_json.get("results"))))

    @cache
    def _call_info(self, charm_name: str) -> dict:
        self.logger.debug(f"Getting info for charm {charm_name}")

        # Formulate request
        request_url = CHARM_INFO_ENDPOINT.format(charm_name=charm_name)
        request_headers = {"Content-Type": "application/json"}
        request_params = {"fields": ",".join(["channel-map"])}

        # Execute request
        response = self.session.get(url=request_url, params=request_params, headers=request_headers, timeout=180)
        response.raise_for_status()
        return response.json()
