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
from typing import TYPE_CHECKING, Any

import requests
import yaml
from pydantic import Field, field_validator, model_validator
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .immutable_dataclass import immutable_dataclass


class UnparsableCharmException(Exception):
    """Raised when the charm cannot be parsed."""

    pass


class NoCharmMetadataException(UnparsableCharmException):
    """Raised when there is no metadata-yaml exposed by the charmhub for this charm."""

    pass


class IncompleteCharmInfoException(UnparsableCharmException):
    """Raised when the charm info from charmhub is incomplete or missing required fields."""

    pass


class CharmReleaseNotFoundException(Exception):
    """Raised when the release for a charm cannot be deduced."""

    pass


@immutable_dataclass
class CharmhubBase:
    architecture: str
    channel: str
    name: str = Field(default="ubuntu")


@immutable_dataclass
class RefreshAction:
    charm_name: str
    charm_revision: int | None = None
    charm_channel: str | None = None
    base: CharmhubBase | None = None
    always_include_base: bool = False


@immutable_dataclass
class CharmMetadata:
    @immutable_dataclass
    class Endpoint:
        interface: str
        optional: bool | None = None
        limit: int | None = None

    peers: dict[str, Endpoint] = Field(default_factory=dict)
    requires: dict[str, Endpoint] = Field(default_factory=dict)
    provides: dict[str, Endpoint] = Field(default_factory=dict)
    assumes: list[str | dict[str, Any]] = Field(default_factory=list)

    if TYPE_CHECKING:  # so mypy knows the class can be constructed from a dict

        def __init__(
            self,
            peers: dict[str, Endpoint | dict[str, Any]] = ...,
            requires: dict[str, Endpoint | dict[str, Any]] = ...,
            provides: dict[str, Endpoint | dict[str, Any]] = ...,
            assumes: list[str | dict[str, Any]] = ...,
        ): ...

    @model_validator(mode="before")
    @classmethod
    def _validate_init(cls, data: Any) -> Any:
        check_dict = None
        if hasattr(data, "args") and hasattr(data, "kwargs"):
            if data.args and len(data.args) == 1 and isinstance(data.args[0], dict) and not data.kwargs:
                check_dict = data.args[0]

        if check_dict:
            for key in ["provides", "requires", "peers"]:
                if key in check_dict:
                    section = check_dict[key]
                    if isinstance(section, dict) and "interface" not in section:
                        return {
                            "peers": check_dict.get("peers", {}),
                            "requires": check_dict.get("requires", {}),
                            "provides": check_dict.get("provides", {}),
                            "assumes": check_dict.get("assumes", []),
                        }
        return data


@immutable_dataclass(config=dict(validate_by_name=True))
class RefreshResponse:
    @immutable_dataclass
    class Charm:
        bases: list[CharmhubBase] | None = None
        revision: int | None = None
        metadata: CharmMetadata = Field(default_factory=CharmMetadata, alias="metadata-yaml")

        @field_validator("metadata", mode="before")
        @classmethod
        def parse_yaml(cls, metadata_yaml: str) -> CharmMetadata:
            return CharmMetadata(**yaml.safe_load(metadata_yaml))

    @immutable_dataclass
    class Error:
        @immutable_dataclass(config=dict(validate_by_name=True))
        class Extra:
            @immutable_dataclass
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


@immutable_dataclass
class FindResponse:
    @immutable_dataclass(config=dict(validate_by_name=True))
    class Result:
        deployable_on: frozenset[str] = Field(default_factory=frozenset, alias="deployable-on")

    name: str
    result: Result


@immutable_dataclass(config=dict(validate_by_name=True))
class InfoResponse:
    @immutable_dataclass
    class DefaultRelease:
        @immutable_dataclass(config=dict(validate_by_name=True))
        class Revision:
            metadata: CharmMetadata = Field(default_factory=CharmMetadata, alias="metadata-yaml")

            @field_validator("metadata", mode="before")
            @classmethod
            def parse_yaml(cls, metadata_yaml: str) -> CharmMetadata:
                return CharmMetadata(**yaml.safe_load(metadata_yaml))

        revision: Revision = Field(default_factory=Revision)

    @immutable_dataclass(config=dict(validate_by_name=True))
    class Result:
        deployable_on: frozenset[str] = Field(default_factory=frozenset, alias="deployable-on")

    default_release: DefaultRelease = Field(default_factory=DefaultRelease, alias="default-release")
    result: Result = Field(default_factory=Result)


CHARM_REFRESH_ENDPOINT = "https://api.charmhub.io/v2/charms/refresh"
CHARM_FIND_ENDPOINT = "https://api.charmhub.io/v2/charms/find"
CHARM_INFO_ENDPOINT = "https://api.charmhub.io/v2/charms/info/{charm}"


class CharmhubHttpClient:
    session: requests.Session
    logger: logging.Logger

    def __init__(
        self, logger: logging.Logger = logging.getLogger(__name__), session: requests.Session | None = None
    ) -> None:
        self.logger = logger

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
            "fields": ["bases", "metadata-yaml", "revision"],
        }

        # Execute request
        response = self.session.post(url=request_url, json=request_body, headers=request_headers, timeout=180)
        response.raise_for_status()
        response_json = response.json()
        return RefreshResponse(**next(iter(response_json.get("results"))))

    @cache
    def info(self, charm: str) -> InfoResponse:
        self.logger.debug(f"Calling info for charm {charm}")

        # Formulate request
        request_url = CHARM_INFO_ENDPOINT.format(charm=charm)
        request_headers = {"Content-Type": "application/json"}
        request_params = {
            "fields": ",".join(
                [
                    "result.deployable-on",
                    "default-release.revision.metadata-yaml",
                ]
            ),
        }

        # Execute request
        response = self.session.get(url=request_url, params=request_params, headers=request_headers, timeout=180)
        response.raise_for_status()
        response_json = response.json()
        return InfoResponse(**response_json)
