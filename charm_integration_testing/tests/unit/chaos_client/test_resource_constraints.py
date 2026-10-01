# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

import pytest
from chaos_client.resource_constraints import (
    CharmResourceConstraints,
    CharmResourceConstraintsFile,
    ResourceConstraintsClient,
)
from pydantic import ValidationError

from bundle_builder_x.charm import CharmChannel


def _ch(track: str, risk: str = "stable") -> CharmChannel:
    return CharmChannel(track=track, risk=risk, branch="")


class TestGetCharmResourceConstraints:
    def test_no_constraints_dir_yields_defaults(self) -> None:
        # GIVEN a client without a constraints directory
        client = ResourceConstraintsClient()

        # THEN every field is unset
        result = client.get_charm_resource_constraints("mysql-k8s", _ch("8.0"), "22.04")
        assert result == CharmResourceConstraints()

    def test_missing_file_yields_defaults(self, tmp_path: Path) -> None:
        # GIVEN a constraints directory with no file for this charm
        client = ResourceConstraintsClient(constraints_dir=tmp_path)

        # THEN every field is unset
        result = client.get_charm_resource_constraints("mysql-k8s", _ch("8.0"), "22.04")
        assert result == CharmResourceConstraints()

    def test_empty_constraints_list_yields_defaults(self, tmp_path: Path) -> None:
        (tmp_path / "mysql-k8s.yaml").write_text("constraints: []\n", encoding="utf-8")
        client = ResourceConstraintsClient(constraints_dir=tmp_path)

        result = client.get_charm_resource_constraints("mysql-k8s", _ch("8.0"), "22.04")
        assert result == CharmResourceConstraints()

    def test_values_are_scoped_to_matching_version(self, tmp_path: Path) -> None:
        # GIVEN an override file where only the track-14 block sets memory-exhaustion constraints
        (tmp_path / "postgresql-k8s.yaml").write_text(
            "constraints:\n"
            "  - criteria:\n"
            "      - track: '14'\n"
            "        ubuntu_version: '22.04'\n"
            "    memory_exhaustion_workers: 2\n"
            "    memory_exhaustion_size_mb: 2048\n"
            "    memory_exhaustion_duration_seconds: 60\n"
            "  - criteria:\n"
            "      - track: '16'\n"
            "    memory_exhaustion_size_mb: 4096\n",
            encoding="utf-8",
        )
        client = ResourceConstraintsClient(constraints_dir=tmp_path)

        # THEN the track-14 block applies only to that channel/base
        track_14 = client.get_charm_resource_constraints("postgresql-k8s", _ch("14"), "22.04")
        assert track_14.memory_exhaustion_workers == 2
        assert track_14.memory_exhaustion_size_mb == 2048
        assert track_14.memory_exhaustion_duration_seconds == 60

        # AND the track-16 block applies to its own channel, with unset fields left as defaults
        track_16 = client.get_charm_resource_constraints("postgresql-k8s", _ch("16"), "22.04")
        assert track_16.memory_exhaustion_size_mb == 4096
        assert track_16.memory_exhaustion_workers is None

        # AND an unmatched channel falls back to defaults
        unmatched = client.get_charm_resource_constraints("postgresql-k8s", _ch("14"), "24.04")
        assert unmatched == CharmResourceConstraints()

    def test_first_matching_block_wins(self, tmp_path: Path) -> None:
        # GIVEN two blocks that could both match
        (tmp_path / "mysql-k8s.yaml").write_text(
            "constraints:\n"
            "  - criteria:\n"
            "      - track: '8.0'\n"
            "    memory_exhaustion_size_mb: 1024\n"
            "  - memory_exhaustion_size_mb: 2048\n",
            encoding="utf-8",
        )
        client = ResourceConstraintsClient(constraints_dir=tmp_path)

        # THEN the first listed match is used
        result = client.get_charm_resource_constraints("mysql-k8s", _ch("8.0"), "22.04")
        assert result.memory_exhaustion_size_mb == 1024

    def test_negative_value_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "mysql-k8s.yaml").write_text("constraints:\n  - memory_exhaustion_size_mb: -1\n", encoding="utf-8")
        client = ResourceConstraintsClient(constraints_dir=tmp_path)

        with pytest.raises(ValidationError):
            client.get_charm_resource_constraints("mysql-k8s", _ch("8.0"), "22.04")

    def test_scenario_fields_are_independent(self, tmp_path: Path) -> None:
        # GIVEN a block that sets both an exhaustion and a moderate-pressure memory scenario
        (tmp_path / "mysql-k8s.yaml").write_text(
            "constraints:\n"
            "  - memory_exhaustion_size_mb: 4096\n"
            "    memory_moderate_pressure_size_mb: 512\n"
            "    cpu_exhaustion_workers: 4\n"
            "    cpu_moderate_pressure_workers: 1\n"
            "    disk_fill_size_mb: 1024\n"
            "    disk_io_latency_delay_ms: 200\n"
            "    disk_io_latency_percent: 50\n",
            encoding="utf-8",
        )
        client = ResourceConstraintsClient(constraints_dir=tmp_path)

        # THEN each scenario's values are read independently of the others
        result = client.get_charm_resource_constraints("mysql-k8s", _ch("8.0"), "22.04")
        assert result.memory_exhaustion_size_mb == 4096
        assert result.memory_moderate_pressure_size_mb == 512
        assert result.cpu_exhaustion_workers == 4
        assert result.cpu_moderate_pressure_workers == 1
        assert result.disk_fill_size_mb == 1024
        assert result.disk_io_latency_delay_ms == 200
        assert result.disk_io_latency_percent == 50

    def test_non_yaml_file_in_directory_is_ignored(self, tmp_path: Path) -> None:
        # GIVEN a README alongside real constraint files
        (tmp_path / "README.md").write_text("not yaml\n", encoding="utf-8")
        (tmp_path / "mysql-k8s.yaml").write_text("constraints:\n  - memory_exhaustion_size_mb: 512\n", encoding="utf-8")
        client = ResourceConstraintsClient(constraints_dir=tmp_path)

        result = client.get_charm_resource_constraints("mysql-k8s", _ch("8.0"), "22.04")
        assert result.memory_exhaustion_size_mb == 512


class TestCharmResourceConstraintsFile:
    def test_ignores_unknown_top_level_keys_by_default(self) -> None:
        # Pydantic ignores unknown fields unless configured otherwise; this test documents that.
        parsed = CharmResourceConstraintsFile(**{"constraints": [], "unexpected": "value"})
        assert parsed.constraints == []
