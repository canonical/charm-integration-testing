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
from pydantic import BaseModel, ConfigDict, Field

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
    model_config = ConfigDict(extra="ignore")
    optional: bool | None = None
    limit: int | None = None


class CharmMetadataFile(BaseModel):
    """Parsed contents of a charm's metadata.yaml override file."""

    requires: dict[str, CharmEndpointOverride] = Field(default_factory=dict)
    provides: dict[str, CharmEndpointOverride] = Field(default_factory=dict)


class CharmConfigsFile(BaseModel):
    """Parsed contents of a charm's configs.yaml override file."""

    configs: list[CharmConfig] = Field(default_factory=list)


class CharmOverrideEntry(BaseModel):
    """One criteria-gated entry inside overrides.yaml."""

    criteria: CharmOverrideCriteria = Field(default_factory=CharmOverrideCriteria)
    metadata: str | None = None  # relative path to metadata.yaml
    configs: str | None = None  # relative path to configs.yaml
    ruleset: str | None = None  # relative path to ruleset.yaml


class CharmOverrideFile(BaseModel):
    """Parsed contents of a charm's overrides.yaml file."""

    platforms: list[str] | None = None
    priority: float | None = None
    listed: bool | None = None
    default_channel: str | None = None
    default_revision: int | None = None
    overrides: list[CharmOverrideEntry] = Field(default_factory=list)


class OverridesClient:
    logger: logging.Logger
    overrides: Path | None = None

    def __init__(
        self,
        logger: logging.Logger = logging.getLogger(__name__),
        overrides: Path | None = None,
    ):
        self.logger = logger
        self.overrides = overrides

    @cache
    def _read_yaml_file(self, path: Path) -> Any:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @cache
    def _get_charm_override_file(self, charm: str) -> CharmOverrideFile:
        if self.overrides is None:
            return CharmOverrideFile()
        path = self.overrides / charm / "overrides.yaml"
        return CharmOverrideFile(**self._read_yaml_file(path))

    @cache
    def _get_charm_override_entry(self, charm: str, channel: CharmChannel) -> CharmOverrideEntry:
        for entry in self._get_charm_override_file(charm).overrides:
            if entry.criteria.meets(channel):
                return entry
        return CharmOverrideEntry()

    def _resolve_relative(self, charm: str, relative_path: str) -> Path:
        """Resolve a path declared in overrides.yaml relative to the charm's directory."""
        return (self.overrides / charm / relative_path).resolve()

    @cache
    def get_charm_metadata_overrides(self, charm: str, channel: CharmChannel) -> CharmMetadataFile:
        entry = self._get_charm_override_entry(charm, channel)
        if entry.metadata is None or self.overrides is None:
            return CharmMetadataFile()
        return CharmMetadataFile(**self._read_yaml_file(self._resolve_relative(charm, entry.metadata)))

    @cache
    def get_charm_platform_overrides(self, charm: str) -> list[str] | None:
        return self._get_charm_override_file(charm).platforms

    @cache
    def get_charm_listing_overrides(self) -> set[str]:
        if self.overrides is None:
            return set()
        unlisted = set()
        for charm_dir in self.overrides.iterdir():
            if not charm_dir.is_dir():
                continue
            override_file = self._get_charm_override_file(charm_dir.name)
            if override_file.listed is False:
                unlisted.add(charm_dir.name)
        return unlisted

    @cache
    def get_charm_configs(self, charm: str, channel: CharmChannel) -> list[CharmConfig]:
        entry = self._get_charm_override_entry(charm, channel)
        if entry.configs is None or self.overrides is None:
            return []
        return CharmConfigsFile(**self._read_yaml_file(self._resolve_relative(charm, entry.configs))).configs

    @cache
    def get_charm_priority(self, charm: str) -> float:
        return self._get_charm_override_file(charm).priority or 1.0

    @cache
    def get_charm_ruleset_url(self, charm: str, channel: CharmChannel) -> str | None:
        entry = self._get_charm_override_entry(charm, channel)
        if entry.ruleset is None or self.overrides is None:
            return None
        return self._resolve_relative(charm, entry.ruleset).as_uri()
    
    @cache
    def get_charm_default_channel(self, charm: str) -> str | None:
        return self._get_charm_override_file(charm).default_channel
    
    @cache
    def get_charm_default_revision(self, charm: str) -> int | None:
        return self._get_charm_override_file(charm).default_revision
