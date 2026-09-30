# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, PositiveInt

from bundle_builder_x.charm import CharmChannel
from bundle_builder_x.overrides import CharmOverridesCriteria


class CharmResourceConstraints(BaseModel):
    """One chaos-parameter block, applied when its ``criteria`` match the deployed version.

    Fields are grouped by chaos scenario, not by ``ChaosClient`` method, because a charm may need
    different values for the "moderate pressure" and "total exhaustion" variant of the same
    underlying stress operation. Only scenarios backed by an implemented ``ChaosClient`` operation
    are represented here; disk I/O saturation has no field yet because no client implements it, and
    network isolation has none because ``isolate_network`` takes no configurable parameters.
    """

    criteria: list[CharmOverridesCriteria] = Field(default_factory=list)

    # CPU total exhaustion (backed by ChaosClient.stress_cpu)
    cpu_exhaustion_workers: PositiveInt | None = None
    cpu_exhaustion_duration_seconds: PositiveInt | None = None

    # CPU moderate pressure (backed by ChaosClient.stress_cpu)
    cpu_moderate_pressure_workers: PositiveInt | None = None
    cpu_moderate_pressure_duration_seconds: PositiveInt | None = None

    # Memory total exhaustion (backed by ChaosClient.stress_memory)
    memory_exhaustion_workers: PositiveInt | None = None
    memory_exhaustion_size_mb: PositiveInt | None = None
    memory_exhaustion_duration_seconds: PositiveInt | None = None

    # Memory moderate pressure (backed by ChaosClient.stress_memory)
    memory_moderate_pressure_workers: PositiveInt | None = None
    memory_moderate_pressure_size_mb: PositiveInt | None = None
    memory_moderate_pressure_duration_seconds: PositiveInt | None = None

    # Disk fill (backed by ChaosClient.fill_disk)
    disk_fill_size_mb: PositiveInt | None = None

    # Disk I/O latency (backed by ChaosClient.io_latency)
    disk_io_latency_delay_ms: PositiveInt | None = None
    disk_io_latency_percent: int | None = Field(default=None, ge=0, le=100)
    disk_io_latency_duration_seconds: PositiveInt | None = None

    def meets(self, channel: CharmChannel, ubuntu_version: str) -> bool:
        return all(criterion.meets(channel, ubuntu_version) for criterion in self.criteria)


class CharmResourceConstraintsFile(BaseModel):
    """Top-level shape of a ``static/charm-resource-constraints/<charm>.yaml`` file."""

    constraints: list[CharmResourceConstraints] = Field(default_factory=list)


class ResourceConstraintsClient:
    """Reads per-charm chaos resource constraints from a directory of YAML files.

    Mirrors :class:`bundle_builder_x.overrides.OverridesClient`, but is kept separate because
    resource constraints are a test-execution concern, not a bundle-construction one.
    """

    logger: logging.Logger
    constraints_dir: Path | None

    def __init__(self, constraints_dir: Path | None = None, logger: logging.Logger = logging.getLogger(__name__)):
        self.constraints_dir = constraints_dir
        self.logger = logger

    @cache
    def _read_yaml_file(self, path: Path) -> Any:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @cache
    def _get_charm_resource_constraints_file(self, charm: str) -> CharmResourceConstraintsFile:
        if self.constraints_dir is None:
            return CharmResourceConstraintsFile()
        path = self.constraints_dir / f"{charm}.yaml"
        return CharmResourceConstraintsFile(**self._read_yaml_file(path))

    def get_charm_resource_constraints(
        self, charm: str, channel: CharmChannel, ubuntu_version: str
    ) -> CharmResourceConstraints:
        """Return the first matching constraints block, or all-defaults if none match."""
        for entry in self._get_charm_resource_constraints_file(charm).constraints:
            if entry.meets(channel, ubuntu_version):
                return entry
        return CharmResourceConstraints()
