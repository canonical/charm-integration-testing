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
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class CharmPlatformOverride(BaseModel):
    platforms: set[str] | None = None


class CharmListingOverrides(BaseModel):
    unlisted_charms: set[str] = Field(default_factory=set)


class CharmPriorities(BaseModel):
    priorities: dict[str, float] = Field(default_factory=dict)  # charm name -> priority


class OverridesClient:
    logger: logging.Logger
    charm_scriptlet_overrides: Path | None = None
    charm_platform_overrides: Path | None = None
    charm_listing_overrides: Path | None = None
    charm_priorities: Path | None = None

    def __init__(
        self,
        logger: logging.Logger = logging.getLogger(__name__),
        charm_scriptlet_overrides: Path | None = None,
        charm_platform_overrides: Path | None = None,
        charm_listing_overrides: Path | None = None,
        charm_priorities: Path | None = None,
    ):
        self.logger = logger
        self.charm_scriptlet_overrides = charm_scriptlet_overrides
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
    def get_charm_scriptlet(self, charm: str) -> str | None:
        # Return None if no scriptlet overrides path given
        if self.charm_scriptlet_overrides is None:
            return None

        # Load scriptlet file
        scriptlet_file = self.charm_scriptlet_overrides / f"{charm}.star"
        if not scriptlet_file.exists():
            return None

        # Read scriptlet content
        try:
            return scriptlet_file.read_text(encoding="utf-8")
        except Exception as e:
            self.logger.error(f"Failed to read scriptlet for charm '{charm}': {e}")
            return None

    @cache
    def get_charm_platform_overrides(self, charm: str) -> set[str] | None:
        return CharmPlatformOverride(**self._read_yaml_file(self.charm_platform_overrides, f"{charm}.yaml")).platforms

    @cache
    def get_charm_listing_overrides(self) -> set[str]:
        return CharmListingOverrides(**self._read_yaml_file(self.charm_listing_overrides, None)).unlisted_charms

    @cache
    def get_charm_priorities(self) -> dict[str, float]:
        return CharmPriorities(**self._read_yaml_file(self.charm_priorities, None)).priorities
