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


@dataclass(frozen=True, config=dict(validate_by_name=True))
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


@dataclass(frozen=True)
class FindResponse:
    @dataclass(frozen=True)
    class Result:
        deployable_on: frozenset[str] = Field(default_factory=frozenset, alias="deployable-on")

    name: str
    result: Result


CHARM_REFRESH_ENDPOINT = "https://api.charmhub.io/v2/charms/refresh"
CHARM_FIND_ENDPOINT = "https://api.charmhub.io/v2/charms/find"


class CharmhubHttpClient:
    session: requests.Session
    logger: logging.Logger

    def __init__(self, logger=logging.getLogger(__name__), session: requests.Session | None = None):
        self.logger = logger

        # Setup requests session with retries
        retry_strategy = Retry(
            total=10,
            status_forcelist=[429, 500, 502, 503, 504],
            backoff_factor=0.5,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session = session or requests.Session()
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    @cache
    def find(self, provides: str | None = None, requires: str | None = None) -> list[FindResponse]:
        self.logger.debug(f"Calling find with provides {provides} and requires {requires}")

        # Formulate request
        request_url = CHARM_FIND_ENDPOINT
        request_headers = {"Content-Type": "application/json"}
        request_params = {
            "q": "",
            "type": "charm",
            "fields": "result.deployable-on",
            **({"provides": provides} if provides is not None else {}),
            **({"requires": requires} if requires is not None else {}),
        }

        # Execute request
        response = self.session.get(url=request_url, params=request_params, headers=request_headers, timeout=180)
        response.raise_for_status()
        response_json = response.json()
        return [FindResponse(**result) for result in response_json.get("results")]

    @cache
    def refresh(self, action: RefreshAction) -> RefreshResponse:
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
