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

from functools import cache
from pathlib import Path

import yaml
from pydantic import Field

from .charm import CharmEndpointOptionality
from .immutable_dataclass import immutable_dataclass


@immutable_dataclass
class CharmEndpointOverride:
    optional: bool | None = None
    optional_if: list[CharmEndpointOptionality] | None = None
    limit: int | None = None
    inject_charm: str | None = None

    def __post_init__(self):
        # Validate limit value if specified
        if self.limit is not None and self.limit < 0:
            raise ValueError(f"Endpoint limit must be non-negative, got: {self.limit}")
        
        # Validate inject_charm is a non-empty string if specified
        if self.inject_charm is not None and not isinstance(self.inject_charm, str):
            raise ValueError("inject_charm must be a string")
        if self.inject_charm is not None and not self.inject_charm.strip():
            raise ValueError("inject_charm cannot be empty")

    @property
    def optionality(self) -> CharmEndpointOptionality | None:
        if self.optional is not None:
            return CharmEndpointOptionality.from_bool(self.optional)
        if self.optional_if is not None:
            return CharmEndpointOptionality(all_of=self.optional_if)
        return None


@immutable_dataclass
class CharmMetadataOverride:
    peers: dict[str, CharmEndpointOverride] = Field(default_factory=dict)
    requires: dict[str, CharmEndpointOverride] = Field(default_factory=dict)
    provides: dict[str, CharmEndpointOverride] = Field(default_factory=dict)


@immutable_dataclass
class CharmPlatformOverride:
    platforms: set[str] = Field(default_factory=set)


@immutable_dataclass
class CharmListingOverrides:
    unlisted_charms: set[str] = Field(default_factory=set)


@immutable_dataclass
class CharmTestConfigs:
    configs: list[dict[str, str | int]] = Field(default_factory=list)


class OverridesClient:
    charm_metadata_overrides: Path | None = None
    charm_platform_overrides: Path | None = None
    charm_listing_overrides: Path | None = None
    charm_test_configs: Path | None = None

    def __init__(
        self,
        charm_metadata_overrides: Path | None = None,
        charm_platform_overrides: Path | None = None,
        charm_listing_overrides: Path | None = None,
        charm_test_configs: Path | None = None,
    ):
        self.charm_metadata_overrides = charm_metadata_overrides
        self.charm_platform_overrides = charm_platform_overrides
        self.charm_listing_overrides = charm_listing_overrides
        self.charm_test_configs = charm_test_configs

    @cache
    def _read_yaml_file(self, path: Path | None, file: str | None) -> dict:
        # Return empty if no path given
        if path is None:
            return {}

        # Get file
        if file is not None:
            path /= file
        if not path.exists():
            return {}

        # Read file
        with path.open("r", encoding="utf-8") as file:
            return yaml.safe_load(file)

    @cache
    def get_charm_metadata_overrides(self, charm: str) -> CharmMetadataOverride:
        return CharmMetadataOverride(**self._read_yaml_file(self.charm_metadata_overrides, f"{charm}.yaml"))

    @cache
    def get_charm_platform_overrides(self, charm: str) -> set[str]:
        return CharmPlatformOverride(**self._read_yaml_file(self.charm_platform_overrides, f"{charm}.yaml")).platforms

    @cache
    def get_charm_listing_overrides(self) -> set[str]:
        return CharmListingOverrides(**self._read_yaml_file(self.charm_listing_overrides, None)).unlisted_charms

    @cache
    def get_charm_test_configs(self, charm: str) -> list[dict]:
        return CharmTestConfigs(**self._read_yaml_file(self.charm_test_configs, f"{charm}.yaml")).configs
