# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

from bundle_builder_x.charm import CharmChannel
from bundle_builder_x.overrides import OverridesClient


def _ch(track: str, risk: str = "stable") -> CharmChannel:
    return CharmChannel(track=track, risk=risk, branch="")


class TestResourceTrackingOverrides:
    def test_skips_are_scoped_to_the_matching_version(self, tmp_path: Path) -> None:
        # GIVEN an override file where only the track-14 block skips PVC tracking
        (tmp_path / "postgresql-k8s.yaml").write_text(
            "overrides:\n"
            "  - criteria:\n"
            "      - track: '14'\n"
            "    resource_tracking:\n"
            "      skip:\n"
            "        - pvc\n"
            "  - criteria:\n"
            "      - track: '16'\n",
            encoding="utf-8",
        )
        client = OverridesClient(overrides=tmp_path)

        # THEN the skip applies to track 14 but not to track 16
        assert client.get_charm_resource_tracking_skips("postgresql-k8s", _ch("14")) == frozenset({"pvc"})
        assert client.get_charm_resource_tracking_skips("postgresql-k8s", _ch("16")) == frozenset()

    def test_missing_section_yields_no_skips(self, tmp_path: Path) -> None:
        # GIVEN an override file with no resource_tracking section
        (tmp_path / "mysql-k8s.yaml").write_text("overrides: []\n", encoding="utf-8")
        client = OverridesClient(overrides=tmp_path)

        # THEN no skips are reported
        assert client.get_charm_resource_tracking_skips("mysql-k8s", _ch("8")) == frozenset()

    def test_no_overrides_directory_yields_no_skips(self) -> None:
        # GIVEN a client without an overrides directory
        client = OverridesClient()

        # THEN no skips are reported
        assert client.get_charm_resource_tracking_skips("postgresql-k8s", _ch("14")) == frozenset()


class TestGetCharmEndpointRemovable:
    def test_endpoint_marked_non_removable(self, tmp_path: Path) -> None:
        # GIVEN an override file that marks a requires endpoint non-removable
        (tmp_path / "mongodb-k8s.yaml").write_text(
            "overrides:\n" "  - requires:\n" "      sharding:\n" "        removable: false\n",
            encoding="utf-8",
        )
        client = OverridesClient(overrides=tmp_path)

        # THEN that endpoint is reported non-removable, and other endpoints are unaffected
        assert client.get_charm_endpoint_removable("mongodb-k8s", _ch("6"), "sharding") is False
        assert client.get_charm_endpoint_removable("mongodb-k8s", _ch("6"), "config-server") is True

    def test_missing_override_defaults_to_removable(self, tmp_path: Path) -> None:
        # GIVEN an override file with no removable declarations
        (tmp_path / "mysql-k8s.yaml").write_text("overrides: []\n", encoding="utf-8")
        client = OverridesClient(overrides=tmp_path)

        # THEN the endpoint defaults to removable
        assert client.get_charm_endpoint_removable("mysql-k8s", _ch("8"), "database") is True

    def test_no_overrides_directory_defaults_to_removable(self) -> None:
        # GIVEN a client without an overrides directory
        client = OverridesClient()

        # THEN the endpoint defaults to removable
        assert client.get_charm_endpoint_removable("mongodb-k8s", _ch("6"), "sharding") is True


class TestGetCharmPriority:
    def test_explicit_zero_priority_is_respected(self, tmp_path: Path) -> None:
        # GIVEN an override file that explicitly sets priority to 0.0
        (tmp_path / "low-priority-charm.yaml").write_text("priority: 0.0\n", encoding="utf-8")
        client = OverridesClient(overrides=tmp_path)

        # THEN the explicit 0.0 is returned, not the 1.0 default
        assert client.get_charm_priority("low-priority-charm") == 0.0

    def test_missing_priority_defaults_to_one(self, tmp_path: Path) -> None:
        # GIVEN an override file with no priority set
        (tmp_path / "no-priority-charm.yaml").write_text("overrides: []\n", encoding="utf-8")
        client = OverridesClient(overrides=tmp_path)

        # THEN the default priority of 1.0 is returned
        assert client.get_charm_priority("no-priority-charm") == 1.0

    def test_no_overrides_directory_defaults_to_one(self) -> None:
        # GIVEN a client without an overrides directory
        client = OverridesClient()

        # THEN the default priority of 1.0 is returned
        assert client.get_charm_priority("any-charm") == 1.0
