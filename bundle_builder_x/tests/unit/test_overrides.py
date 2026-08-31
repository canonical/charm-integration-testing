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


class TestGetCharmClusterAddons:
    def test_cluster_addons_are_scoped_to_the_matching_version(self, tmp_path: Path) -> None:
        # GIVEN an override file declaring a cluster addon only for track 1
        (tmp_path / "istio-beacon-k8s.yaml").write_text(
            "overrides:\n"
            "  - criteria:\n"
            "      - track: '1'\n"
            "    cluster_addons:\n"
            "      - charm: istio-k8s\n"
            "        channel: 1/stable\n",
            encoding="utf-8",
        )
        client = OverridesClient(overrides=tmp_path)

        # THEN the addon is reported for track 1 but not for other tracks
        addons = client.get_charm_cluster_addons("istio-beacon-k8s", _ch("1"))
        assert [(a.charm, a.channel) for a in addons] == [("istio-k8s", "1/stable")]
        assert client.get_charm_cluster_addons("istio-beacon-k8s", _ch("2")) == []

    def test_missing_section_yields_no_addons(self, tmp_path: Path) -> None:
        # GIVEN an override file with no cluster_addons section
        (tmp_path / "grafana-k8s.yaml").write_text("overrides: []\n", encoding="utf-8")
        client = OverridesClient(overrides=tmp_path)

        # THEN no addons are reported
        assert client.get_charm_cluster_addons("grafana-k8s", _ch("14")) == []

    def test_no_overrides_directory_yields_no_addons(self) -> None:
        # GIVEN a client without an overrides directory
        client = OverridesClient()

        # THEN no addons are reported
        assert client.get_charm_cluster_addons("istio-beacon-k8s", _ch("1")) == []


class TestGetCharmAddonScope:
    def test_explicit_model_scope_is_respected(self, tmp_path: Path) -> None:
        # GIVEN an override file that explicitly scopes the addon to the dependent model
        (tmp_path / "some-addon.yaml").write_text("addon_scope: model\n", encoding="utf-8")
        client = OverridesClient(overrides=tmp_path)

        # THEN the explicit "model" scope is returned, not the "cluster" default
        assert client.get_charm_addon_scope("some-addon") == "model"

    def test_missing_scope_defaults_to_cluster(self, tmp_path: Path) -> None:
        # GIVEN an override file with no addon_scope set
        (tmp_path / "istio-k8s.yaml").write_text("overrides: []\n", encoding="utf-8")
        client = OverridesClient(overrides=tmp_path)

        # THEN the default scope of "cluster" is returned
        assert client.get_charm_addon_scope("istio-k8s") == "cluster"

    def test_no_overrides_directory_defaults_to_cluster(self) -> None:
        # GIVEN a client without an overrides directory
        client = OverridesClient()

        # THEN the default scope of "cluster" is returned
        assert client.get_charm_addon_scope("any-charm") == "cluster"
