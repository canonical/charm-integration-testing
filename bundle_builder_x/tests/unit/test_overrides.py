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
