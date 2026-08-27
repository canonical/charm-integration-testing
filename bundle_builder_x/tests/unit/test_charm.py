# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from bundle_builder_x.charm import (
    Charm,
    CharmAssumesEntry,
    CharmChannel,
    CharmEndpoint,
    EndpointScope,
    EndpointType,
)
from bundle_builder_x.juju_version import JujuVersion


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


class TestCharmAssumesEntry:
    def test_satisfaction_matches_reported_failures(self) -> None:
        entry = CharmAssumesEntry(
            all_of=frozenset(
                {
                    CharmAssumesEntry(feature="k8s-api"),
                    CharmAssumesEntry(op=">=", required_version=JujuVersion.parse("4.0.0")),
                }
            )
        )

        assert entry.satisfied_by(JujuVersion.parse("3.6.0"), frozenset({"juju"})) is False
        assert entry.satisfied_by(JujuVersion.parse("4.0.0"), frozenset({"juju", "k8s-api"})) is True

    def test_reports_missing_feature(self) -> None:
        entry = CharmAssumesEntry(feature="k8s-api")

        assert entry.unsatisfied_requirements(
            JujuVersion.parse("3.6.0"),
            frozenset({"juju"}),
        ) == ("feature=k8s-api",)

    def test_reports_failed_juju_version(self) -> None:
        entry = CharmAssumesEntry(op=">=", required_version=JujuVersion.parse("4.0.0"))

        assert entry.unsatisfied_requirements(
            JujuVersion.parse("3.6.0"),
            frozenset({"juju"}),
        ) == ("juju>=4.0.0",)

    def test_all_of_reports_each_failed_leaf(self) -> None:
        entry = CharmAssumesEntry(
            all_of=frozenset(
                {
                    CharmAssumesEntry(feature="k8s-api"),
                    CharmAssumesEntry(feature="foo"),
                }
            )
        )

        assert entry.unsatisfied_requirements(
            JujuVersion.parse("3.6.0"),
            frozenset({"juju"}),
        ) == ("feature=foo", "feature=k8s-api")

    def test_any_of_preserves_the_failed_alternative_group(self) -> None:
        entry = CharmAssumesEntry(
            any_of=frozenset(
                {
                    CharmAssumesEntry(feature="k8s-api"),
                    CharmAssumesEntry(op=">=", required_version=JujuVersion.parse("4.0.0")),
                }
            )
        )

        assert entry.unsatisfied_requirements(
            JujuVersion.parse("3.6.0"),
            frozenset({"juju"}),
        ) == ("any-of(feature=k8s-api,juju>=4.0.0)",)


class TestCharmEndpointScope:
    """CharmEndpoint.scope field."""

    def test_scope_defaults_to_none(self) -> None:
        ep = CharmEndpoint(type=EndpointType.REQUIRES, interface="http")
        assert ep.scope is None

    def test_scope_stored_when_provided(self) -> None:
        ep = CharmEndpoint(type=EndpointType.REQUIRES, interface="juju-info", scope=EndpointScope.CONTAINER)
        assert ep.scope == EndpointScope.CONTAINER

    def test_scope_global_stored(self) -> None:
        ep = CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", scope=EndpointScope.GLOBAL)
        assert ep.scope == EndpointScope.GLOBAL


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
            platforms=["machine", "kubernetes"],
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
            platforms=["machine", "kubernetes"],
        )
        assert charm.subordinate is True
