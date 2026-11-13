# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from dataclasses import replace

import pytest

from bundle_builder_x.bundle_builder import BundleBuilder
from bundle_builder_x.candidate_discovery import CandidateDiscovery, DependencyAlternative, ReleaseRequirements
from bundle_builder_x.charm import CharmChannel, CharmEndpoint, EndpointScope, EndpointType
from bundle_builder_x.domain import DomainModel, ModelRef
from bundle_builder_x.juju_version import JujuVersion
from bundle_builder_x.spec import AppSpec

from .conftest import StrictChannelCatalog, build_single_model, make_charm


@pytest.mark.parametrize("channel", [None, "1/stable/testing"], ids=["unpinned", "explicit-branch"])
def test_solver_only_discovers_branch_releases_when_requested(channel: str | None) -> None:
    # GIVEN a compatible branch on the initial track and a regular release on another track
    worker = make_charm(
        "worker",
        ubuntu_version="20.04",
        endpoints={"host": CharmEndpoint(type=EndpointType.PROVIDES, interface="host", optional=False)},
    )
    initial = make_charm(
        "cni",
        channel="1/stable",
        ubuntu_version="24.04",
        subordinate=True,
        endpoints={"host": CharmEndpoint(type=EndpointType.REQUIRES, interface="host", scope=EndpointScope.CONTAINER)},
    )
    branch = make_charm(
        "cni",
        channel="1/stable/testing",
        ubuntu_version="20.04",
        revision=2,
        subordinate=True,
        endpoints=initial.endpoints,
    )
    regular = make_charm(
        "cni",
        channel="2/stable",
        ubuntu_version="20.04",
        revision=3,
        subordinate=True,
        endpoints=initial.endpoints,
    )

    # WHEN the real solver builds with either no channel pin or an explicit branch
    bundle = build_single_model(
        BundleBuilder(charmhub_client=StrictChannelCatalog(worker, initial, branch, regular)),
        applications={"worker": AppSpec(charm="worker", base="20.04"), "cni": AppSpec(charm="cni", channel=channel)},
        platform="machine",
    )

    # THEN only the explicit selector permits choosing the branch
    assert set(bundle.applications) == {"worker", "cni"}
    assert bundle.applications["cni"].charm == (branch if channel else regular)
    assert len(bundle.integrations) == 1


def test_solver_tries_preferred_releases_before_enumerating_other_channels() -> None:
    # GIVEN many alternative channels, but a stable release already satisfies the constraints
    class PreferredCatalog(StrictChannelCatalog):
        def get_charm_channels(self, charm_name: str) -> list[CharmChannel]:
            raise AssertionError("Preferred releases suffice; channel enumeration is unnecessary")

    worker = make_charm(
        "worker",
        channel="2/stable",
        ubuntu_version="20.04",
        endpoints={"cni": CharmEndpoint(type=EndpointType.PROVIDES, interface="cni", optional=False)},
        constraint_strs=["tracks(charms(endpoint[cni])) <= tracks({self})"],
    )
    initial = make_charm(
        "cni",
        channel="2/stable",
        ubuntu_version="24.04",
        subordinate=True,
        endpoints={"host": CharmEndpoint(type=EndpointType.REQUIRES, interface="cni", scope=EndpointScope.CONTAINER)},
    )
    releases = [
        make_charm(
            "cni",
            channel=f"{track}/{risk}",
            ubuntu_version="20.04",
            revision=track,
            subordinate=True,
            endpoints=initial.endpoints,
        )
        for track in range(2, 12)
        for risk in ("stable", "beta", "edge")
    ]

    # WHEN track and base mismatches are resolved by the real solver
    bundle = build_single_model(
        BundleBuilder(charmhub_client=PreferredCatalog(worker, initial, *releases)),
        applications={"worker": AppSpec(charm="worker", channel="2/stable", base="20.04"), "cni": AppSpec(charm="cni")},
        platform="machine",
    )

    # THEN the preferred stable release is kept, without adding unnecessary applications
    assert set(bundle.applications) == {"worker", "cni"}
    assert bundle.applications["cni"].charm == releases[0]
    assert len(bundle.integrations) == 1


def test_requirements_accumulate_between_discovery_iterations() -> None:
    # GIVEN releases that each satisfy another discovery requirement
    initial = make_charm("cni", channel="1/stable", ubuntu_version="24.04")
    risk_match = make_charm("cni", channel="1/edge", ubuntu_version="24.04", revision=2)
    track_match = make_charm("cni", channel="2/edge", ubuntu_version="24.04", revision=3)
    full_match = make_charm("cni", channel="2/edge", ubuntu_version="20.04", revision=4)
    discovery = CandidateDiscovery(StrictChannelCatalog(initial, risk_match, track_match, full_match))
    alternative = DependencyAlternative(ModelRef(name="model"), 0, "cni", 1)
    model = DomainModel(arch="amd64", platform="machine", juju_version=JujuVersion(major=3, minor=6, patch=0))

    # WHEN requirements arrive separately, THEN each search retains the earlier requirements
    current = initial
    for requirement, expected in [
        (ReleaseRequirements(risk="edge"), risk_match),
        (ReleaseRequirements(track="2"), track_match),
        (ReleaseRequirements(base="20.04"), full_match),
    ]:
        current = next(discovery.candidates(alternative, model, current, requirement, [ReleaseRequirements()]))
        assert current == expected


def test_different_dependency_alternatives_do_not_share_requirements() -> None:
    # GIVEN two anchors that need different releases of the same dependency
    initial = make_charm("cni", channel="1/stable", ubuntu_version="24.04")
    other = make_charm("cni", channel="2/stable", ubuntu_version="20.04", revision=2)
    discovery = CandidateDiscovery(StrictChannelCatalog(initial, other))
    first = DependencyAlternative(ModelRef(name="model"), 0, "cni", 1)
    second = replace(first, anchor_id=2)
    model = DomainModel(arch="amd64", platform="machine", juju_version=JujuVersion(major=3, minor=6, patch=0))

    # WHEN each alternative is searched, THEN its own compatible release is found
    assert (
        next(discovery.candidates(first, model, initial, ReleaseRequirements(track="1"), [ReleaseRequirements()]))
        == initial
    )
    assert (
        next(discovery.candidates(second, model, initial, ReleaseRequirements(base="20.04"), [ReleaseRequirements()]))
        == other
    )


def test_conflicting_hints_remain_alternatives_but_pins_are_hard_restrictions() -> None:
    # GIVEN an earlier hint for a different track
    initial = make_charm("cni", channel="1/stable", ubuntu_version="24.04")
    other = make_charm("cni", channel="2/stable", ubuntu_version="20.04", revision=2)
    discovery = CandidateDiscovery(StrictChannelCatalog(initial, other))
    alternative = DependencyAlternative(ModelRef(name="model"), 0, "cni", 1)
    model = DomainModel(arch="amd64", platform="machine", juju_version=JujuVersion(major=3, minor=6, patch=0))
    list(discovery.candidates(alternative, model, initial, ReleaseRequirements(track="1"), [ReleaseRequirements()]))

    # WHEN another track is requested, THEN both alternatives remain available
    candidates = list(
        discovery.candidates(alternative, model, initial, ReleaseRequirements(track="2"), [ReleaseRequirements()])
    )
    assert initial in candidates and other in candidates
    # A user pin still excludes releases from the other track.
    pinned = list(
        discovery.candidates(
            alternative, model, initial, ReleaseRequirements(track="2"), [ReleaseRequirements(track="1")]
        )
    )
    assert pinned and all(charm.channel.explicit_track == "1" for charm in pinned)


def test_solver_discovers_candidate_matching_track_risk_and_base() -> None:
    # GIVEN partial matches and one release satisfying all three constraints
    worker = make_charm(
        "worker",
        channel="2/edge",
        ubuntu_version="20.04",
        endpoints={"cni": CharmEndpoint(type=EndpointType.PROVIDES, interface="cni", optional=False)},
        constraint_strs=[
            "tracks(charms(endpoint[cni])) <= tracks({self})",
            "risks(charms(endpoint[cni])) <= risks({self})",
        ],
    )
    releases = [
        make_charm(
            "cni",
            channel=channel,
            ubuntu_version=base,
            revision=revision,
            subordinate=True,
            endpoints={
                "host": CharmEndpoint(type=EndpointType.REQUIRES, interface="cni", scope=EndpointScope.CONTAINER)
            },
        )
        for revision, (channel, base) in enumerate(
            [
                ("1/stable", "24.04"),
                ("1/edge", "24.04"),
                ("2/stable", "24.04"),
                ("2/edge", "24.04"),
                ("2/edge", "20.04"),
            ],
            start=1,
        )
    ]
    # WHEN the solver expands the initially incompatible dependency
    bundle = build_single_model(
        BundleBuilder(charmhub_client=StrictChannelCatalog(worker, *releases)),
        applications={"worker": AppSpec(charm="worker", channel="2/edge", base="20.04"), "cni": AppSpec(charm="cni")},
        platform="machine",
    )

    # THEN it selects the complete match without adding a duplicate application
    assert set(bundle.applications) == {"worker", "cni"}
    assert bundle.applications["cni"].charm == releases[-1]
    assert [tuple(map(str, integration.endpoints)) for integration in bundle.integrations] == [
        ("cni:host", "worker:cni")
    ]


@pytest.mark.parametrize("match_risk", [True, False], ids=["risk-and-revision", "revision-only"])
def test_solver_preserves_revision_constraints_without_inventing_a_risk(match_risk: bool) -> None:
    # GIVEN partial matches for risk and revision, plus an optional complete match
    constraints = ["revisions(charms(endpoint[cni])) <= revisions({self})"]
    if match_risk:
        constraints.append("risks(charms(endpoint[cni])) <= risks({self})")
    owner = make_charm(
        "owner",
        channel="2/edge",
        revision=7,
        endpoints={"cni": CharmEndpoint(type=EndpointType.PROVIDES, interface="cni", optional=False)},
        constraint_strs=constraints,
    )
    initial = make_charm(
        "peer",
        channel="2/edge",
        revision=8,
        endpoints={"host": CharmEndpoint(type=EndpointType.REQUIRES, interface="cni")},
    )
    revision_match = make_charm("peer", channel="2/stable", revision=7, endpoints=initial.endpoints)
    complete_match = make_charm("peer", channel="2/edge", revision=7, endpoints=initial.endpoints)
    releases = [owner, initial, revision_match]
    if match_risk:
        releases.append(complete_match)

    # WHEN the real solver resolves an unpinned peer
    bundle = build_single_model(
        BundleBuilder(charmhub_client=StrictChannelCatalog(*releases)),
        applications={"owner": AppSpec(charm="owner", channel="2/edge", revision=7), "peer": AppSpec(charm="peer")},
    )

    # THEN only the requested dimensions restrict the selected release
    assert set(bundle.applications) == {"owner", "peer"}
    assert bundle.applications["peer"].charm == (complete_match if match_risk else revision_match)
    assert len(bundle.integrations) == 1


@pytest.mark.parametrize("channel", ["stable", "latest/stable", "stable/testing"])
def test_subordinate_expansion_preserves_revision_and_channel_pins(channel: str) -> None:
    # GIVEN the pinned revision on both bases and a newer revision on the required base
    worker = make_charm(
        "worker",
        ubuntu_version="20.04",
        endpoints={"host": CharmEndpoint(type=EndpointType.PROVIDES, interface="host", optional=False)},
    )
    initial = make_charm(
        "subordinate",
        channel=channel,
        revision=7,
        ubuntu_version="24.04",
        subordinate=True,
        endpoints={"host": CharmEndpoint(type=EndpointType.REQUIRES, interface="host", scope=EndpointScope.CONTAINER)},
    )
    compatible = initial.model_copy(update={"ubuntu_version": "20.04"})
    newer = compatible.model_copy(update={"revision": 8})

    # WHEN the subordinate must move to the principal's base
    bundle = build_single_model(
        BundleBuilder(charmhub_client=StrictChannelCatalog(worker, initial, newer, compatible)),
        applications={
            "worker": AppSpec(charm="worker", base="20.04"),
            "subordinate": AppSpec(charm="subordinate", channel=channel, revision=7),
        },
        platform="machine",
    )

    # THEN the older revision and channel (including its branch) remain pinned
    assert set(bundle.applications) == {"worker", "subordinate"}
    assert bundle.applications["subordinate"].charm == compatible
    assert len(bundle.integrations) == 1
