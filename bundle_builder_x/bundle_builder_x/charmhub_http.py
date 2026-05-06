# Copyright (C) 2026 Canonical Ltd

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
from typing import Any

import requests
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class UnparsableCharmException(Exception):
    """Raised when the charm cannot be parsed."""

    pass


class IncompleteCharmInfoException(UnparsableCharmException):
    """Raised when the charm info from charmhub is incomplete or missing required fields."""

    pass


class CharmReleaseNotFoundException(Exception):
    """Raised when the release for a charm cannot be deduced."""

    pass


class CharmhubBase(BaseModel):
    model_config = ConfigDict(frozen=True)

    architecture: str
    channel: str
    name: str = Field(default="ubuntu")


class RefreshAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    charm_name: str
    charm_revision: int | None = None
    charm_channel: str | None = None
    base: CharmhubBase | None = None
    always_include_base: bool = False


class CharmMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    class Endpoint(BaseModel):
        model_config = ConfigDict(frozen=True)

        interface: str
        optional: bool | None = None
        limit: int | None = None

    peers: dict[str, Endpoint] = Field(default_factory=dict)
    requires: dict[str, Endpoint] = Field(default_factory=dict)
    provides: dict[str, Endpoint] = Field(default_factory=dict)
    assumes: list[str | dict[str, Any]] = Field(default_factory=list)


class CharmConfigSchema(BaseModel):
    model_config = ConfigDict(frozen=True)

    class Option(BaseModel):
        model_config = ConfigDict(frozen=True)

        type: str
        default: Any = None

    options: dict[str, "Option"] = Field(default_factory=dict)


class RefreshResponse(BaseModel):
    model_config = ConfigDict(frozen=True, validate_by_name=True)

    class Charm(BaseModel):
        model_config = ConfigDict(frozen=True)

        bases: list[CharmhubBase] | None = None
        revision: int | None = None
        metadata: CharmMetadata = Field(default_factory=CharmMetadata, alias="metadata-yaml")
        config: CharmConfigSchema = Field(default_factory=CharmConfigSchema, alias="config-yaml")

        @field_validator("metadata", mode="before")
        @classmethod
        def parse_metadata_yaml(cls, metadata_yaml: str) -> CharmMetadata:
            return CharmMetadata(**yaml.safe_load(metadata_yaml))

        @field_validator("config", mode="before")
        @classmethod
        def parse_config_yaml(cls, config_yaml: str) -> CharmConfigSchema:
            return CharmConfigSchema(**yaml.safe_load(config_yaml))

    class Error(BaseModel):
        model_config = ConfigDict(frozen=True)

        class Extra(BaseModel):
            model_config = ConfigDict(frozen=True, validate_by_name=True)

            class Release(BaseModel):
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


class FindResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    class Result(BaseModel):
        model_config = ConfigDict(frozen=True)

        deployable_on: set[str] = Field(default_factory=set, alias="deployable-on")

    name: str
    result: Result


class InfoResponse(BaseModel):
    model_config = ConfigDict(frozen=True, validate_by_name=True)

    class DefaultRelease(BaseModel):
        model_config = ConfigDict(frozen=True)

        class Revision(BaseModel):
            model_config = ConfigDict(frozen=True)

            metadata: CharmMetadata = Field(default_factory=CharmMetadata, alias="metadata-yaml")

            @field_validator("metadata", mode="before")
            @classmethod
            def parse_yaml(cls, metadata_yaml: str) -> CharmMetadata:
                return CharmMetadata(**yaml.safe_load(metadata_yaml))

        revision: Revision = Field(default_factory=Revision)

    class ChannelMapEntry(BaseModel):
        model_config = ConfigDict(frozen=True, validate_by_name=True)

        class Channel(BaseModel):
            model_config = ConfigDict(frozen=True)

            name: str
            track: str
            risk: str

        channel: Channel

    class Result(BaseModel):
        model_config = ConfigDict(frozen=True, validate_by_name=True)

        deployable_on: frozenset[str] = Field(default_factory=frozenset, alias="deployable-on")

    default_release: DefaultRelease = Field(default_factory=DefaultRelease, alias="default-release")
    channel_map: list["InfoResponse.ChannelMapEntry"] = Field(default_factory=list, alias="channel-map")
    result: Result = Field(default_factory=Result)


CHARM_REFRESH_ENDPOINT = "https://api.charmhub.io/v2/charms/refresh"
CHARM_FIND_ENDPOINT = "https://api.charmhub.io/v2/charms/find"
CHARM_INFO_ENDPOINT = "https://api.charmhub.io/v2/charms/info/{charm}"


class CharmhubHttpClient:
    session: requests.Session
    logger: logging.Logger
    timeout: int

    def __init__(
        self,
        logger: logging.Logger = logging.getLogger(__name__),
        session: requests.Session | None = None,
        timeout: int = 180,
    ) -> None:
        self.logger = logger
        self.timeout = timeout

        # Setup requests session with retries
        retry_strategy = Retry(
            total=10,
            status_forcelist=[408, 429, 500, 502, 503, 504],
            allowed_methods=Retry.DEFAULT_ALLOWED_METHODS | {"GET", "POST"},
            backoff_factor=0.5,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session = session if session is not None else requests.Session()
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
        response = self.session.get(
            url=request_url, params=request_params, headers=request_headers, timeout=self.timeout
        )
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
        action_dict: dict[str, Any] = {"name": action.charm_name}
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
            "fields": ["bases", "metadata-yaml", "revision", "config-yaml"],
        }

        # Execute request
        response = self.session.post(url=request_url, json=request_body, headers=request_headers, timeout=self.timeout)
        response.raise_for_status()
        response_json = response.json()
        return RefreshResponse(**next(iter(response_json.get("results"))))

    @cache
    def info(self, charm: str, include_channel_map: bool = False) -> InfoResponse:
        self.logger.debug(f"Calling info for charm {charm}")

        # Formulate request
        request_url = CHARM_INFO_ENDPOINT.format(charm=charm)
        request_headers = {"Content-Type": "application/json"}
        fields = [
            "result.deployable-on",
            "default-release.revision.metadata-yaml",
        ]
        if include_channel_map:
            fields.append("channel-map")
        request_params = {"fields": ",".join(fields)}

        # Execute request
        response = self.session.get(
            url=request_url, params=request_params, headers=request_headers, timeout=self.timeout
        )
        response.raise_for_status()
        response_json = response.json()
        return InfoResponse(**response_json)
