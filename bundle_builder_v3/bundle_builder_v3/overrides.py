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
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .charm import CharmChannel, CharmConfig


class CharmOverrideCriteria(BaseModel):
    any_of: list["CharmOverrideCriteria"] | None = None
    all_of: list["CharmOverrideCriteria"] | None = None
    none_of: list["CharmOverrideCriteria"] | None = None
    track: str | None = None
    risk: str | None = None

    def meets(self, channel: CharmChannel) -> bool:
        return all(
            (
                all(criterion.meets(channel) for criterion in self.all_of) if self.all_of else True,
                not any(criterion.meets(channel) for criterion in self.none_of) if self.none_of else True,
                any(criterion.meets(channel) for criterion in self.any_of) if self.any_of else True,
                channel.explicit_track == self.track if self.track else True,
                channel.risk == self.risk if self.risk else True,
            )
        )


class CharmEndpointOverride(BaseModel):
    optional: bool | None = None
    limit: int | None = None
    cyclic: bool | None = None


class CharmMetadataOverride(BaseModel):
    criteria: CharmOverrideCriteria = Field(default_factory=CharmOverrideCriteria)
    peers: dict[str, CharmEndpointOverride] = Field(default_factory=dict)
    requires: dict[str, CharmEndpointOverride] = Field(default_factory=dict)
    provides: dict[str, CharmEndpointOverride] = Field(default_factory=dict)
    constraints: str | None = None


class CharmMetadataOverrides(BaseModel):
    overrides: list[CharmMetadataOverride] = Field(default_factory=list)


class CharmPlatformOverrides(BaseModel):
    platforms: set[str] | None = None


class CharmListingOverrides(BaseModel):
    unlisted_charms: set[str] = Field(default_factory=set)


class CharmTestConfig(BaseModel):
    criteria: CharmOverrideCriteria = Field(default_factory=CharmOverrideCriteria)
    config: CharmConfig = Field(default_factory=dict)


class CharmTestConfigs(BaseModel):
    configs: list[CharmTestConfig] = Field(default_factory=list)


class CharmPriorities(BaseModel):
    priorities: dict[str, float] = Field(default_factory=dict)


class OverridesClient:
    logger: logging.Logger
    charm_metadata_overrides: Path | None = None
    charm_platform_overrides: Path | None = None
    charm_listing_overrides: Path | None = None
    charm_test_configs: Path | None = None
    charm_priorities: Path | None = None

    def __init__(
        self,
        logger: logging.Logger = logging.getLogger(__name__),
        charm_metadata_overrides: Path | None = None,
        charm_platform_overrides: Path | None = None,
        charm_listing_overrides: Path | None = None,
        charm_test_configs: Path | None = None,
        charm_priorities: Path | None = None,
    ):
        self.logger = logger
        self.charm_metadata_overrides = charm_metadata_overrides
        self.charm_platform_overrides = charm_platform_overrides
        self.charm_listing_overrides = charm_listing_overrides
        self.charm_test_configs = charm_test_configs
        self.charm_priorities = charm_priorities

    @cache
    def _read_yaml_file(self, path: Path | None, file_name: str | None) -> Any:
        # Return empty if no path given
        if path is None:
            return {}

        # Get file
        if file_name is not None:
            path /= file_name
        if not path.exists():
            return {}

        # Read file
        with path.open("r", encoding="utf-8") as file:
            return yaml.safe_load(file)

    @cache
    def get_charm_metadata_overrides(self, charm: str, channel: CharmChannel) -> CharmMetadataOverride:
        for override in CharmMetadataOverrides(
            **self._read_yaml_file(self.charm_metadata_overrides, f"{charm}.yaml")
        ).overrides:
            if override.criteria.meets(channel):
                return override
        return CharmMetadataOverride()

    @cache
    def get_charm_platform_overrides(self, charm: str) -> set[str] | None:
        return CharmPlatformOverrides(**self._read_yaml_file(self.charm_platform_overrides, f"{charm}.yaml")).platforms

    @cache
    def get_charm_listing_overrides(self) -> set[str]:
        return CharmListingOverrides(**self._read_yaml_file(self.charm_listing_overrides, None)).unlisted_charms

    @cache
    def get_charm_test_configs(self, charm: str, channel: CharmChannel) -> list[CharmConfig]:
        return [
            config.config
            for config in CharmTestConfigs(**self._read_yaml_file(self.charm_test_configs, f"{charm}.yaml")).configs
            if config.criteria.meets(channel)
        ]

    @cache
    def get_charm_priorities(self) -> dict[str, float]:
        return CharmPriorities(**self._read_yaml_file(self.charm_priorities, None)).priorities
