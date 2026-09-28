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
        # GIVEN an override file where only the track-14 block sets memory constraints
        (tmp_path / "postgresql-k8s.yaml").write_text(
            "constraints:\n"
            "  - criteria:\n"
            "      - track: '14'\n"
            "        ubuntu_version: '22.04'\n"
            "    stress_memory_workers: 2\n"
            "    stress_memory_size_mb: 2048\n"
            "    duration_seconds: 60\n"
            "  - criteria:\n"
            "      - track: '16'\n"
            "    stress_memory_size_mb: 4096\n",
            encoding="utf-8",
        )
        client = ResourceConstraintsClient(constraints_dir=tmp_path)

        # THEN the track-14 block applies only to that channel/base
        track_14 = client.get_charm_resource_constraints("postgresql-k8s", _ch("14"), "22.04")
        assert track_14.stress_memory_workers == 2
        assert track_14.stress_memory_size_mb == 2048
        assert track_14.duration_seconds == 60

        # AND the track-16 block applies to its own channel, with unset fields left as defaults
        track_16 = client.get_charm_resource_constraints("postgresql-k8s", _ch("16"), "22.04")
        assert track_16.stress_memory_size_mb == 4096
        assert track_16.stress_memory_workers is None

        # AND an unmatched channel falls back to defaults
        unmatched = client.get_charm_resource_constraints("postgresql-k8s", _ch("14"), "24.04")
        assert unmatched == CharmResourceConstraints()

    def test_first_matching_block_wins(self, tmp_path: Path) -> None:
        # GIVEN two blocks that could both match
        (tmp_path / "mysql-k8s.yaml").write_text(
            "constraints:\n"
            "  - criteria:\n"
            "      - track: '8.0'\n"
            "    stress_memory_size_mb: 1024\n"
            "  - stress_memory_size_mb: 2048\n",
            encoding="utf-8",
        )
        client = ResourceConstraintsClient(constraints_dir=tmp_path)

        # THEN the first listed match is used
        result = client.get_charm_resource_constraints("mysql-k8s", _ch("8.0"), "22.04")
        assert result.stress_memory_size_mb == 1024

    def test_negative_value_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "mysql-k8s.yaml").write_text("constraints:\n  - stress_memory_size_mb: -1\n", encoding="utf-8")
        client = ResourceConstraintsClient(constraints_dir=tmp_path)

        with pytest.raises(ValidationError):
            client.get_charm_resource_constraints("mysql-k8s", _ch("8.0"), "22.04")

    def test_non_yaml_file_in_directory_is_ignored(self, tmp_path: Path) -> None:
        # GIVEN a README alongside real constraint files
        (tmp_path / "README.md").write_text("not yaml\n", encoding="utf-8")
        (tmp_path / "mysql-k8s.yaml").write_text("constraints:\n  - stress_memory_size_mb: 512\n", encoding="utf-8")
        client = ResourceConstraintsClient(constraints_dir=tmp_path)

        result = client.get_charm_resource_constraints("mysql-k8s", _ch("8.0"), "22.04")
        assert result.stress_memory_size_mb == 512


class TestCharmResourceConstraintsFile:
    def test_rejects_unknown_top_level_keys_are_ignored_by_default(self) -> None:
        # Pydantic ignores unknown fields unless configured otherwise; this test documents that.
        parsed = CharmResourceConstraintsFile(**{"constraints": [], "unexpected": "value"})
        assert parsed.constraints == []
