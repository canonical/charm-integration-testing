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
