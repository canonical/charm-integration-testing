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
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .charm import CharmConstraints


class CharmMetadataVariant(BaseModel):
    track: str | None = None
    risk: str | None = None
    constraints: CharmConstraints

class CharmMetadataOverrides(BaseModel):
    variants: list[CharmMetadataVariant] | None = None


class CharmPlatformOverride(BaseModel):
    platforms: set[str] | None = None


class CharmListingOverrides(BaseModel):
    unlisted_charms: set[str] = Field(default_factory=set)


class CharmPriorities(BaseModel):
    priorities: dict[str, float] = Field(default_factory=dict)  # charm name -> priority


class OverridesClient:
    charm_metadata_overrides: Path | None = None
    charm_platform_overrides: Path | None = None
    charm_listing_overrides: Path | None = None
    charm_priorities: Path | None = None

    def __init__(
        self,
        charm_metadata_overrides: Path | None = None,
        charm_platform_overrides: Path | None = None,
        charm_listing_overrides: Path | None = None,
        charm_priorities: Path | None = None,
    ):
        self.charm_metadata_overrides = charm_metadata_overrides
        self.charm_platform_overrides = charm_platform_overrides
        self.charm_listing_overrides = charm_listing_overrides
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
    def get_charm_metadata_overrides(self, charm: str) -> list[CharmMetadataVariant] | None:
        return CharmMetadataOverrides(**self._read_yaml_file(self.charm_metadata_overrides, f"{charm}.yaml")).variants

    @cache
    def get_charm_platform_overrides(self, charm: str) -> set[str] | None:
        return CharmPlatformOverride(**self._read_yaml_file(self.charm_platform_overrides, f"{charm}.yaml")).platforms

    @cache
    def get_charm_listing_overrides(self) -> set[str]:
        return CharmListingOverrides(**self._read_yaml_file(self.charm_listing_overrides, None)).unlisted_charms

    @cache
    def get_charm_priorities(self) -> dict[str, float]:
        return CharmPriorities(**self._read_yaml_file(self.charm_priorities, None)).priorities
