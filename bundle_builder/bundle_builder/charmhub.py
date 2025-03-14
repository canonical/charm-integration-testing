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
from functools import cache

import requests
import yaml

from .charm import ENDPOINT_PEERS, ENDPOINT_PROVIDES, ENDPOINT_REQUIRES, Charm, CharmEndpoint


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
class RefreshAction:
    @dataclass(frozen=True)
    class Base:
        name: str = "ubuntu"
        version: str | None = None
        arch: str | None = None

    charm_name: str
    charm_revision: int | None = None
    charm_channel: str | None = None
    base: Base | None = None
    always_include_base: bool = False


CHARM_INFO_ENDPOINT = "https://api.charmhub.io/v2/charms/info/{charm_name}"
CHARM_REFRESH_ENDPOINT = "https://api.charmhub.io/v2/charms/refresh"
CHARM_STORE_JSON_ENDPOINT = "https://charmhub.io/store.json"


class CharmhubClient:
    requests: any
    logger: logging.Logger

    def __init__(self, logger=logging.getLogger(__name__)):
        self.requests = requests
        self.logger = logger

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
    def find_charms(self, provides: str | None = None, platform: str | None = None) -> frozenset[str]:
        # Get store JSON
        response = self._call_store_json(provides=provides)

        # Parse response
        charms = {
            package.get("package", {}).get("name", None)
            for package in response.get("packages", [])
            if platform in package.get("package", {}).get("platforms", [])
        }

        # Return charms
        return frozenset({charm for charm in charms if charm is not None})

    def _charm_from_store_by_revision(
        self,
        charm_name: str,
        ubuntu_arch: str,
        charm_revision: int,
        ubuntu_version: str = None,
    ) -> Charm:
        # Call refresh with revision and null base
        refresh_info = self._call_refresh(
            RefreshAction(
                charm_name=charm_name,
                charm_revision=charm_revision,
                always_include_base=True,
            )
        )

        # Find channel from info endpoint
        # Because bundles always requires channel and /refresh does not provide it when queried by revision :/
        charm_channel = self._channel_from_revision(charm_name, charm_revision)

        # Return Charm from refresh info
        return Charm(
            name=charm_name,
            channel=charm_channel,
            revision=charm_revision,
            ubuntu_version=ubuntu_version,
            ubuntu_arch=ubuntu_arch,
            endpoints=self._endpoints_from_refresh_info(refresh_info),
        )

    def _charm_from_store_by_channel(
        self,
        charm_name: str,
        ubuntu_arch: str,
        charm_channel: str,
        ubuntu_version: str = None,
    ):
        # Get default ubuntu version if not given
        if not ubuntu_version:
            ubuntu_version = self._default_ubuntu_version(charm_name, ubuntu_arch)

        # Call refresh with channel and base
        refresh_info = self._call_refresh(
            RefreshAction(
                charm_name=charm_name,
                charm_channel=charm_channel,
                base=RefreshAction.Base(
                    version=ubuntu_version,
                    arch=ubuntu_arch,
                ),
            )
        )

        # Return Charm
        return Charm(
            name=charm_name,
            channel=charm_channel,
            revision=refresh_info.get("charm", {}).get("revision"),
            ubuntu_version=ubuntu_version,
            ubuntu_arch=ubuntu_arch,
            endpoints=self._endpoints_from_refresh_info(refresh_info),
        )

    def _charm_from_store_default(
        self,
        charm_name: str,
        ubuntu_arch: str,
        ubuntu_version: str = None,
    ):
        # Get default ubuntu version if not given
        if not ubuntu_version:
            ubuntu_version = self._default_ubuntu_version(charm_name, ubuntu_arch)

        # Call refresh with base
        refresh_info = self._call_refresh(
            RefreshAction(
                charm_name=charm_name,
                base=RefreshAction.Base(
                    version=ubuntu_version,
                    arch=ubuntu_arch,
                ),
            )
        )

        # Return Charm
        return Charm(
            name=charm_name,
            channel=refresh_info.get("effective-channel"),
            revision=refresh_info.get("charm", {}).get("revision"),
            ubuntu_version=ubuntu_version,
            ubuntu_arch=ubuntu_arch,
            endpoints=self._endpoints_from_refresh_info(refresh_info),
        )

    def _default_ubuntu_version(self, charm_name: str, ubuntu_arch: str) -> str:
        # Juju passes "NA" to get the secret "default-bases" error field
        default_bases = (
            self._call_refresh(
                RefreshAction(
                    charm_name=charm_name,
                    base=RefreshAction.Base(
                        name="NA",
                        version="NA",
                        arch=ubuntu_arch,
                    ),
                )
            )
            .get("error", {})
            .get("extra", {})
            .get("default-bases", [])
        )

        # Ensure a base was found
        if len(default_bases) == 0:
            raise CharmReleaseNotFoundException(
                f"No default bases found for {charm_name} in architecture {ubuntu_arch}"
            )

        # Pick the first base (like Juju)
        return default_bases[0].get("channel", None)

    def _channel_from_revision(self, charm_name: str, charm_revision: int):
        # Call info endpoint
        charm_info = self._call_info(charm_name)

        # Search for revision
        for release in charm_info.get("channel-map", []):
            # Check revision
            if release.get("revision", {}).get("revision", {}) != charm_revision:
                continue

            # Return channel if found
            channel = release.get("channel", {}).get("name", None)
            if channel is not None:
                return channel

        # Raise not found
        # A channel is required to deploy a charm with Juju, even when pinned to a revision
        raise CharmReleaseNotFoundException(f"Revision {charm_revision} of {charm_name} not found in any channel")

    @cache
    def _call_store_json(self, provides: str | None = None) -> dict:
        self.logger.debug(f"Calling store json with provides {provides}")

        # Formulate request
        request_url = CHARM_STORE_JSON_ENDPOINT
        request_headers = {"Content-Type": "application/json"}
        request_params = {"size": 300, "type": "charm", **({"provides": provides if provides is not None else {}})}

        # Execute request
        response = self.requests.get(url=request_url, params=request_params, headers=request_headers, timeout=180)
        response.raise_for_status()
        return response.json()

    @cache
    def _call_refresh(self, action: RefreshAction) -> dict:
        self.logger.debug(f"Calling refresh for charm {action.charm_name}")

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
                "channel": action.base.version,
                "architecture": action.base.arch,
            }
        elif action.always_include_base:
            action_dict["base"] = None
        request_body = {
            "context": [],
            "actions": [{"action": "install", "instance-key": "1", **action_dict}],
            "fields": ["metadata-yaml", "effective-channel", "revision"],
        }

        # Execute request
        response = self.requests.post(url=request_url, json=request_body, headers=request_headers, timeout=180)
        response.raise_for_status()
        response_json = response.json()
        return next(iter(response_json.get("results", {})), {})

    @cache
    def _call_info(self, charm_name: str) -> dict:
        self.logger.debug(f"Getting info for charm {charm_name}")

        # Formulate request
        request_url = CHARM_INFO_ENDPOINT.format(charm_name=charm_name)
        request_headers = {"Content-Type": "application/json"}
        request_params = {"fields": ",".join(["channel-map"])}

        # Execute request
        response = self.requests.get(url=request_url, params=request_params, headers=request_headers, timeout=180)
        response.raise_for_status()
        return response.json()

    def _metadata_from_refresh_info(self, refresh_info: dict) -> dict:
        try:
            return yaml.safe_load(refresh_info["charm"]["metadata-yaml"])
        except KeyError:
            raise NoCharmMetadataException(
                f"[ERROR] Charm {refresh_info.get('name')} does not expose a metadata-yaml key. Notify this error to SQA!"
            )

    def _endpoints_from_refresh_info(self, refresh_info: dict) -> frozenset[CharmEndpoint]:
        # Get metadata
        metadata = self._metadata_from_refresh_info(refresh_info)

        # Return endpoints
        return frozenset(
            {
                CharmEndpoint(
                    type=endpoint_type,
                    name=endpoint_name,
                    interface=endpoint.get("interface"),
                    optional=endpoint.get("optional", False),
                )
                for endpoint_type in {ENDPOINT_PEERS, ENDPOINT_REQUIRES, ENDPOINT_PROVIDES}
                for endpoint_name, endpoint in metadata.get(endpoint_type, {}).items()
            }
        )
