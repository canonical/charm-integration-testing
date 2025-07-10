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
from pydantic.dataclasses import dataclass

from .charm import CharmEndpointOptionality


@dataclass
class CharmEndpointOverride:
    optional: bool | None = None
    optional_if: list[CharmEndpointOptionality] | None = None

    @property
    def optionality(self) -> CharmEndpointOptionality | None:
        if self.optional is not None:
            return CharmEndpointOptionality.from_bool(self.optional)
        if self.optional_if is not None:
            return CharmEndpointOptionality(all_of=self.optional_if)
        return None


@dataclass
class CharmMetadataOverride:
    peers: dict[str, CharmEndpointOverride] = Field(default_factory=dict)
    requires: dict[str, CharmEndpointOverride] = Field(default_factory=dict)
    provides: dict[str, CharmEndpointOverride] = Field(default_factory=dict)


@dataclass
class CharmPlatformOverride:
    platforms: set[str] = Field(default_factory=set)


@dataclass
class CharmListingOverrides:
    unlisted_charms: set[str] = Field(default_factory=set)


class OverridesClient:
    charm_metadata_overrides: Path | None = None
    charm_platform_overrides: Path | None = None
    charm_listing_overrides: Path | None = None

    def __init__(
        self,
        charm_metadata_overrides: Path | None = None,
        charm_platform_overrides: Path | None = None,
        charm_listing_overrides: Path | None = None,
    ):
        self.charm_metadata_overrides = charm_metadata_overrides
        self.charm_platform_overrides = charm_platform_overrides
        self.charm_listing_overrides = charm_listing_overrides

    @cache
    def get_charm_metadata_overrides(self, charm: str) -> CharmMetadataOverride:
        # Return empty if no charm metadata overrides folder given
        if self.charm_metadata_overrides is None:
            return CharmMetadataOverride()

        # Get override file
        override_file = self.charm_metadata_overrides / f"{charm}.yaml"
        if not override_file.exists():
            return CharmMetadataOverride()

        # Read override file
        with override_file.open("r", encoding="utf-8") as file:
            return CharmMetadataOverride(**yaml.safe_load(file))

    @cache
    def get_charm_platform_overrides(self, charm: str) -> set[str]:
        # Return empty if no charm platform overrides folder given
        if self.charm_platform_overrides is None:
            return set()

        # Get override file
        override_file = self.charm_platform_overrides / f"{charm}.yaml"
        if not override_file.exists():
            return set()

        # Read override file
        with override_file.open("r", encoding="utf-8") as file:
            return CharmPlatformOverride(**yaml.safe_load(file)).platforms

    @cache
    def get_charm_listing_overrides(self) -> set[str]:
        # Return empty if no charm listing overrides folder given
        if self.charm_listing_overrides is None:
            return set()

        # Get override file
        override_file = self.charm_listing_overrides
        if not override_file.exists():
            return set()

        # Read override file
        with override_file.open("r", encoding="utf-8") as file:
            return CharmListingOverrides(**yaml.safe_load(file)).unlisted_charms
