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

import json
import logging

import requests
import yaml


class UnparsableCharmException(Exception):
    """Raised when the charm cannot be parsed."""

    pass


class NoCharmMetadataException(UnparsableCharmException):
    """Raised when there is no metadata-yaml exposed by the charmhub for this charm."""

    pass


class Charm:
    name: str
    channel: str
    revision: int
    ubuntu_version: str
    ubuntu_arch: str
    peer_integrations: dict
    requires_integrations: dict
    provides_integrations: dict
    LOGGER: logging.Logger

    def __init__(
        self,
        name,
        channel,
        revision,
        ubuntu_version,
        ubuntu_arch,
        peer_integrations,
        requires_integrations,
        provides_integrations,
        logger,
    ):
        self.name = name
        self.channel = channel
        self.revision = revision
        self.ubuntu_version = ubuntu_version
        self.ubuntu_arch = ubuntu_arch
        self.peer_integrations = peer_integrations
        self.requires_integrations = requires_integrations
        self.provides_integrations = provides_integrations
        self.LOGGER = logger

    def __repr__(self):
        return self.name

    @property
    def non_optional_peers(self):
        return list(filter(lambda item: not item.get("optional", False), self.peer_integrations))

    @property
    def non_optional_requires(self):
        return list(filter(lambda item: not item.get("optional", False), self.requires_integrations))

    @property
    def non_optional_provides(self):
        return list(filter(lambda item: not item.get("optional", False), self.provides_integrations))

    @classmethod
    def from_store(
        cls,
        charm_name,
        charm_channel="latest",
        charm_revision=None,
        ubuntu_version=None,
        ubuntu_arch=None,
        logger=logging.getLogger(__name__),
    ):
        CHARM_REFRESH_ENDPOINT = "https://api.charmhub.io/v2/charms/refresh"
        request_headers = {"Content-Type": "application/json"}

        data = {"context": [], "actions": [], "fields": ["metadata-yaml", "effective-channel", "revision"]}
        action_object = {"action": "install", "instance-key": "1", "name": charm_name, "base": None}

        if charm_channel and charm_revision:
            logger.error("Both charm_channel and charm_revision passed to charm initialization. Using charm revision")
            action_object["revision"] = charm_revision
        elif charm_channel:
            action_object["channel"] = charm_channel
        elif charm_revision:
            action_object["revision"] = charm_revision

        if ubuntu_version and ubuntu_arch:
            logger.debug("Appending base as ubuntu version and arch were passed.")
            action_object["base"] = {
                "name": "ubuntu",
                "channel": ubuntu_version,
                "architecture": ubuntu_arch,
            }

        data["actions"] = [action_object]

        data = json.dumps(data)

        resp = requests.post(url=CHARM_REFRESH_ENDPOINT, data=data, headers=request_headers, timeout=180)
        resp.raise_for_status()
        json_resp = resp.json()
        requested_charm_info = next(iter(json_resp.get("results", {})), {})
        charm_info = requested_charm_info.get("charm", {})
        metadata_yaml = charm_info.get("metadata-yaml")

        try:
            metadata_yaml = yaml.safe_load(metadata_yaml)
        except KeyError:
            raise NoCharmMetadataException(
                f"[ERROR] Charm {charm_name} does not expose a metadata-yaml key. Notify this error to SQA!"
            )

        requires_possible_integrations = [
            dict(endpoint_name=requiresk, **requiresv)
            for requiresk, requiresv in metadata_yaml.get("requires", {}).items()
        ]

        provides_possible_integrations = [
            dict(endpoint_name=providesk, **providesv)
            for providesk, providesv in metadata_yaml.get("provides", {}).items()
        ]

        peer_possible_integrations = [
            dict(endpoint_name=peersk, **peersv) for peersk, peersv in metadata_yaml.get("peers", {}).items()
        ]

        return cls(
            name=metadata_yaml.get("name", ""),
            channel=requested_charm_info.get("effective-channel", None),
            revision=charm_info.get("revision", None),
            ubuntu_version=ubuntu_version,
            ubuntu_arch=ubuntu_arch,
            peer_integrations=peer_possible_integrations,
            requires_integrations=requires_possible_integrations,
            provides_integrations=provides_possible_integrations,
            logger=logger,
        )

    @classmethod
    def from_store_default(cls, charm_name, logger=logging.getLogger(__name__)):
        CHARM_INFO_ENDPOINT = "https://api.charmhub.io/v2/charms/info/{charm}?fields=type,id,name,default-release"
        resp = requests.get(CHARM_INFO_ENDPOINT.format(charm=charm_name), timeout=180)
        resp.raise_for_status()
        json_resp = resp.json()
        default_release_info = json_resp["default-release"]["channel"]
        return cls.from_store(
            charm_name=charm_name,
            charm_channel=default_release_info["name"],
            ubuntu_arch=default_release_info["base"]["architecture"],
            ubuntu_version=default_release_info["base"]["channel"],
            logger=logger,
        )

    def is_equal(self, value):
        if isinstance(value, Charm):
            # XXX: For the moment, lets only check equality by name and channel;
            # XXX: something weird happens when you try to check for revision/arch/ubuntu_version
            return all(
                [
                    self.name == value.name,
                    self.channel == value.channel,
                    # self.revision == value.revision,
                    # self.ubuntu_version == value.ubuntu_version,
                    # self.ubuntu_arch == value.ubuntu_arch
                ]
            )
        return False
