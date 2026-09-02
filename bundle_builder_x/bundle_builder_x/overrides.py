# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .charm import CharmChannel, CharmConfigValue, CharmEndpointProxy, CharmResourceValue, EndpointType
from .timing import NullTimeline, Timeline


class CharmOverridesCriteria(BaseModel):
    any_of: list["CharmOverridesCriteria"] | None = None
    all_of: list["CharmOverridesCriteria"] | None = None
    none_of: list["CharmOverridesCriteria"] | None = None
    track: str | None = None
    risk: str | None = None
    # Some charms (e.g. kubernetes-worker) publish substantially different metadata
    # (different provides/requires endpoints) for the *same* track depending on the
    # Ubuntu base - typically because an older charm generation is still served on
    # legacy bases under a track that has since moved to a rewritten charm for newer
    # bases. track/risk alone cannot express that split, so ubuntu_version lets an
    # override block additionally require an exact base match (e.g. '22.04').
    ubuntu_version: str | None = None

    def meets(self, channel: CharmChannel, ubuntu_version: str | None = None) -> bool:
        return all(
            (
                all(criterion.meets(channel, ubuntu_version) for criterion in self.all_of) if self.all_of else True,
                not any(criterion.meets(channel, ubuntu_version) for criterion in self.none_of)
                if self.none_of
                else True,
                any(criterion.meets(channel, ubuntu_version) for criterion in self.any_of) if self.any_of else True,
                channel.explicit_track == self.track if self.track else True,
                channel.risk == self.risk if self.risk else True,
                ubuntu_version == self.ubuntu_version if self.ubuntu_version else True,
            )
        )


class CharmEndpointOverrides(BaseModel):
    optional: bool | None = None
    limit: int | None = None
    cyclic: bool | None = None
    features: set[str] = Field(default_factory=set)


class CharmResourceTrackingOverrides(BaseModel):
    # Resource *kinds* (e.g. ``pvc``) that the integration-test resource tracker
    # should not flag as drift for this charm version.  Some charms legitimately
    # leave resources behind across removals or scale events.
    skip: set[str] = Field(default_factory=set)


class CharmOverrides(BaseModel):
    criteria: list[CharmOverridesCriteria] = Field(default_factory=list)
    requires: dict[str, CharmEndpointOverrides] = Field(default_factory=dict)
    provides: dict[str, CharmEndpointOverrides] = Field(default_factory=dict)
    proxies: list[CharmEndpointProxy] = Field(default_factory=list)
    configs: dict[str, list[CharmConfigValue]] = Field(default_factory=dict)
    resources: dict[str, list[CharmResourceValue]] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    assumes: list[str | dict[str, Any]] | None = None
    resource_tracking: CharmResourceTrackingOverrides = Field(default_factory=CharmResourceTrackingOverrides)

    def meets(self, channel: CharmChannel, ubuntu_version: str | None = None) -> bool:
        return all(criterion.meets(channel, ubuntu_version) for criterion in self.criteria)


class CharmGlobalOverrides(BaseModel):
    platforms: list[str] | None = None
    priority: float | None = None
    listed: bool | None = None
    default_channel: str | None = None
    default_revision: int | None = None
    overrides: list[CharmOverrides] = Field(default_factory=list)


class OverridesClient:
    logger: logging.Logger
    overrides: Path | None = None
    timeline: Timeline

    def __init__(
        self,
        logger: logging.Logger = logging.getLogger(__name__),
        overrides: Path | None = None,
        timeline: Timeline | None = None,
    ):
        self.logger = logger
        self.overrides = overrides
        self.timeline = (timeline if timeline is not None else NullTimeline()).child("overrides")

    @cache
    def _read_yaml_file(self, path: Path) -> Any:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @cache
    def _get_charm_global_overrides(self, charm: str) -> CharmGlobalOverrides:
        if self.overrides is None:
            return CharmGlobalOverrides()
        token = self.timeline.on(f"read/{charm}")
        path = self.overrides / f"{charm}.yaml"
        result = CharmGlobalOverrides(**self._read_yaml_file(path))
        self.timeline.off(token)
        return result

    @cache
    def _get_all_charm_global_overrides(self) -> dict[str, CharmGlobalOverrides]:
        if self.overrides is None:
            return {}
        token = self.timeline.on("read_all")
        overrides = {}
        for charm_file in self.overrides.iterdir():
            if not charm_file.is_file() or charm_file.suffix != ".yaml":
                continue
            charm_name = charm_file.stem
            overrides[charm_name] = self._get_charm_global_overrides(charm_name)
        self.timeline.off(token)
        return overrides

    def _get_charm_overrides(
        self, charm: str, channel: CharmChannel, ubuntu_version: str | None = None
    ) -> CharmOverrides:
        for entry in self._get_charm_global_overrides(charm).overrides:
            if entry.meets(channel, ubuntu_version):
                return entry
        return CharmOverrides()

    def get_charm_endpoint_overrides(
        self, charm: str, channel: CharmChannel, ubuntu_version: str | None = None
    ) -> dict[EndpointType, dict[str, CharmEndpointOverrides]]:
        overrides = self._get_charm_overrides(charm, channel, ubuntu_version)
        return {
            EndpointType.REQUIRES: overrides.requires,
            EndpointType.PROVIDES: overrides.provides,
        }

    def get_charm_proxy_overrides(
        self, charm: str, channel: CharmChannel, ubuntu_version: str | None = None
    ) -> list[CharmEndpointProxy]:
        return self._get_charm_overrides(charm, channel, ubuntu_version).proxies

    def get_charm_config_overrides(
        self, charm: str, channel: CharmChannel, ubuntu_version: str | None = None
    ) -> dict[str, list[CharmConfigValue]]:
        return self._get_charm_overrides(charm, channel, ubuntu_version).configs

    def get_charm_resource_overrides(
        self, charm: str, channel: CharmChannel, ubuntu_version: str | None = None
    ) -> dict[str, list[CharmResourceValue]]:
        return self._get_charm_overrides(charm, channel, ubuntu_version).resources

    def get_charm_constraints_overrides(
        self, charm: str, channel: CharmChannel, ubuntu_version: str | None = None
    ) -> list[str]:
        return self._get_charm_overrides(charm, channel, ubuntu_version).constraints

    def get_charm_resource_tracking_skips(self, charm: str, channel: CharmChannel) -> frozenset[str]:
        return frozenset(self._get_charm_overrides(charm, channel).resource_tracking.skip)

    def get_charm_assumes_overrides(
        self, charm: str, channel: CharmChannel, ubuntu_version: str | None = None
    ) -> list[str | dict[str, Any]] | None:
        return self._get_charm_overrides(charm, channel, ubuntu_version).assumes

    def get_charm_platform_overrides(self, charm: str) -> list[str] | None:
        return self._get_charm_global_overrides(charm).platforms

    def get_charm_listing_overrides(self) -> set[str]:
        return {
            charm for charm, overrides in self._get_all_charm_global_overrides().items() if overrides.listed is True
        }

    def get_charm_delisting_overrides(self) -> set[str]:
        return {
            charm for charm, overrides in self._get_all_charm_global_overrides().items() if overrides.listed is False
        }

    def get_charm_priority(self, charm: str) -> float:
        priority = self._get_charm_global_overrides(charm).priority
        return priority if priority is not None else 1.0

    def get_charm_default_channel(self, charm: str) -> str | None:
        return self._get_charm_global_overrides(charm).default_channel

    def get_charm_default_revision(self, charm: str) -> int | None:
        return self._get_charm_global_overrides(charm).default_revision
