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

from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointType


def _ch(track: str, risk: str) -> CharmChannel:
    return CharmChannel(track=track, risk=risk, branch="")


class TestCharmChannel:
    class TestOrdering:
        def test_stable_before_candidate(self) -> None:
            # GIVEN two channels on the same track differing only by risk
            # THEN stable sorts before candidate
            assert _ch("latest", "stable") < _ch("latest", "candidate")

        def test_candidate_before_beta(self) -> None:
            assert _ch("latest", "candidate") < _ch("latest", "beta")

        def test_beta_before_edge(self) -> None:
            assert _ch("latest", "beta") < _ch("latest", "edge")

        def test_track_sorts_alphabetically(self) -> None:
            # GIVEN channels on tracks "1.0" and "2.0"
            # THEN alphabetical order applies
            assert _ch("1.0", "stable") < _ch("2.0", "stable")

        def test_sorted_produces_correct_order(self) -> None:
            # GIVEN an unsorted list of channels
            channels = [
                _ch("latest", "edge"),
                _ch("1.0", "stable"),
                _ch("latest", "stable"),
                _ch("1.0", "edge"),
            ]
            # WHEN sorted
            result = sorted(channels)
            # THEN alphabetical track first, then stable-first risk within track
            assert result == [
                _ch("1.0", "stable"),
                _ch("1.0", "edge"),
                _ch("latest", "stable"),
                _ch("latest", "edge"),
            ]


class TestCharmEndpointScope:
    """CharmEndpoint.scope field."""

    def test_scope_defaults_to_none(self) -> None:
        ep = CharmEndpoint(type=EndpointType.REQUIRES, interface="http")
        assert ep.scope is None

    def test_scope_stored_when_provided(self) -> None:
        ep = CharmEndpoint(type=EndpointType.REQUIRES, interface="juju-info", scope="container")
        assert ep.scope == "container"

    def test_scope_global_stored(self) -> None:
        ep = CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", scope="global")
        assert ep.scope == "global"


class TestCharmSubordinateField:
    """Charm.subordinate field."""

    def test_defaults_to_false(self) -> None:
        charm = Charm(
            name="test",
            channel=_ch("latest", "stable"),
            revision=1,
            ubuntu_version="22.04",
            ubuntu_arch="amd64",
            endpoints={},
        )
        assert charm.subordinate is False

    def test_can_be_set_true(self) -> None:
        charm = Charm(
            name="nrpe",
            channel=_ch("latest", "stable"),
            revision=1,
            ubuntu_version="22.04",
            ubuntu_arch="amd64",
            subordinate=True,
            endpoints={},
        )
        assert charm.subordinate is True
