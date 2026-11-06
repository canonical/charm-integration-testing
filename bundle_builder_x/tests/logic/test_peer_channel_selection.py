# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Full-build regressions for peer channel mismatches across several candidate peers.

The worker mirrors kubernetes-worker: its override requires every CNI peer on the worker's own
track. Several CNI subordinates are auto-discovered, but the solver reports only one of them first,
and which one depends on solver internals. The synthetic catalogs simplify the observed failure:
the only 20.04 build on track 1.33 is on edge, and one candidate has no 20.04 build there at all.
"""

import itertools

import pytest

from bundle_builder_x.bundle_builder import BundleBuilder, UncompletableBundleError
from bundle_builder_x.charm import Charm, CharmEndpoint, EndpointScope, EndpointType
from bundle_builder_x.spec import AppSpec

from .conftest import StrictChannelCatalog, build_single_model, make_charm

_TRACK_CONSTRAINT = "tracks(charms(endpoint[cni])) <= tracks({self})"


def _worker(channel: str, base: str = "20.04") -> Charm:
    return make_charm(
        "worker",
        constraint_strs=[_TRACK_CONSTRAINT],
        endpoints={
            "cni": CharmEndpoint(
                type=EndpointType.PROVIDES, interface="kubernetes-cni", optional=False, scope=EndpointScope.CONTAINER
            ),
        },
        channel=channel,
        ubuntu_version=base,
    )


def _cni(name: str, channel: str, base: str, revision: int) -> Charm:
    return make_charm(
        name,
        endpoints={
            "cni": CharmEndpoint(
                type=EndpointType.REQUIRES, interface="kubernetes-cni", optional=False, scope=EndpointScope.CONTAINER
            ),
        },
        channel=channel,
        ubuntu_version=base,
        revision=revision,
        subordinate=True,
    )


def _build(catalog: list[Charm], **app_overrides: AppSpec) -> dict[str, Charm]:
    bundle = build_single_model(
        BundleBuilder(charmhub_client=StrictChannelCatalog(*catalog)),
        applications={"worker": AppSpec(charm="worker", channel="1.33/edge", base="20.04"), **app_overrides},
        platform="machine",
    )
    return {name: app.charm for name, app in bundle.applications.items()}


class TestFirstReportedPeerHasNoCompatibleRelease:
    """A peer with no release on the required track and base must not hide a peer that has one."""

    @staticmethod
    def _catalog(without_release: str, with_release: str, reverse: bool) -> list[Charm]:
        charms = [
            _worker("1.33/edge"),
            # Only a 24.04 build exists on the required track, so the 20.04 principal cannot host it
            _cni(without_release, "latest/stable", "20.04", 1),
            _cni(without_release, "1.33/stable", "24.04", 2),
            _cni(with_release, "latest/stable", "20.04", 3),
            _cni(with_release, "1.33/edge", "20.04", 4),
        ]
        return charms[::-1] if reverse else charms

    @pytest.mark.parametrize(
        ("without_release", "with_release"),
        itertools.permutations(["alpha-cni", "zulu-cni"]),
        ids=["bad-sorts-first", "bad-sorts-last"],
    )
    @pytest.mark.parametrize("reverse", [False, True], ids=["catalog-order", "reversed-catalog"])
    def test_peer_with_a_compatible_release_is_selected(
        self, without_release: str, with_release: str, reverse: bool
    ) -> None:
        charms = _build(self._catalog(without_release, with_release, reverse))

        chosen = charms[with_release]
        assert (str(chosen.channel), chosen.ubuntu_version, chosen.revision) == ("1.33/edge", "20.04", 4)
        if without_release in charms:
            assert charms[without_release].revision != 2

    @pytest.mark.parametrize(
        "pin",
        [
            AppSpec(charm="zulu-cni", channel="latest/stable"),
            AppSpec(charm="zulu-cni", revision=3),
            AppSpec(charm="zulu-cni", base="22.04"),
        ],
        ids=["channel", "revision", "base"],
    )
    def test_explicitly_pinned_peer_is_not_moved(self, pin: AppSpec) -> None:
        with pytest.raises(UncompletableBundleError):
            _build(self._catalog("alpha-cni", "zulu-cni", reverse=False), **{"zulu-cni": pin})

    def test_no_peer_with_a_compatible_release_is_still_rejected(self) -> None:
        catalog = [c for c in self._catalog("alpha-cni", "zulu-cni", reverse=False) if c.revision != 4]

        with pytest.raises(UncompletableBundleError):
            _build(catalog)


@pytest.mark.parametrize(
    "pin",
    [
        AppSpec(charm="owner", channel="1/stable"),
        AppSpec(charm="owner", revision=1),
        AppSpec(charm="owner", base="22.04"),
    ],
    ids=["channel", "revision", "base"],
)
@pytest.mark.parametrize("forward", [False, True], ids=["owner-fallback", "peer-fallback"])
def test_fallback_does_not_add_an_application_that_bypasses_pins(pin: AppSpec, forward: bool) -> None:
    def owner(channel: str, revision: int, base: str) -> Charm:
        return make_charm(
            "owner",
            channel=channel,
            revision=revision,
            ubuntu_version=base,
            constraint_strs=[] if forward else ["tracks(charms(endpoint[mesh])) <= tracks({self})"],
            endpoints={"mesh": CharmEndpoint(type=EndpointType.PROVIDES, interface="mesh", optional=True)},
        )

    peer = make_charm(
        "peer",
        channel="2/stable",
        constraint_strs=["tracks(charms(endpoint[mesh])) <= tracks({self})"] if forward else [],
        endpoints={"mesh": CharmEndpoint(type=EndpointType.REQUIRES, interface="mesh", optional=False)},
    )
    catalog = StrictChannelCatalog(owner("1/stable", 1, "22.04"), owner("2/stable", 2, "24.04"), peer)

    # The optional endpoint lets a duplicate satisfy the mandatory endpoint while the
    # pinned application stays disconnected. Exercise both directions of the fallback.
    with pytest.raises(UncompletableBundleError):
        build_single_model(
            BundleBuilder(charmhub_client=catalog),
            applications={"owner": pin, "peer": AppSpec(charm="peer", channel="2/stable")},
            platform="machine",
        )


@pytest.mark.parametrize("pin", [AppSpec(charm="peer", revision=1), AppSpec(charm="peer", base="22.04")])
def test_peer_lookup_finds_pinned_release_instead_of_channel_default(pin: AppSpec) -> None:
    owner = make_charm(
        "owner",
        channel="2/stable",
        constraint_strs=["tracks(charms(endpoint[mesh])) <= tracks({self})"],
        endpoints={"mesh": CharmEndpoint(type=EndpointType.PROVIDES, interface="mesh")},
    )
    initial = make_charm(
        "peer",
        channel="1/stable",
        revision=1,
        endpoints={"mesh": CharmEndpoint(type=EndpointType.REQUIRES, interface="mesh", optional=True)},
    )
    compatible = initial.model_copy(update={"channel": owner.channel})
    default = compatible.model_copy(update={"revision": 2, "ubuntu_version": "24.04"})
    bundle = build_single_model(
        BundleBuilder(charmhub_client=StrictChannelCatalog(owner, initial, default, compatible)),
        applications={"owner": AppSpec(charm="owner", channel="2/stable"), "peer": pin},
        platform="machine",
    )
    assert set(bundle.applications) == {"owner", "peer"}
    chosen = bundle.applications["peer"].charm
    assert (str(chosen.channel), chosen.revision, chosen.ubuntu_version) == ("2/stable", 1, "22.04")


class TestPrincipalMovesToALaterPeersChannel:
    """When no peer can reach the principal's track, the principal may follow any peer, not only the first reported."""

    @staticmethod
    def _catalog(first: str, second: str) -> list[Charm]:
        return [
            # The unpinned principal defaults to 1.33, which no peer offers on 20.04; it has no 1.32 build
            _worker("1.33/stable"),
            _worker("1.34/stable"),
            _cni(first, "1.32/stable", "20.04", 1),
            _cni(first, "1.34/stable", "20.04", 2),
            _cni(second, "1.34/stable", "20.04", 3),
        ]

    @pytest.mark.parametrize(("first", "second"), itertools.permutations(["alpha-cni", "zulu-cni"]))
    def test_principal_follows_the_peer_channel_it_can_reach(self, first: str, second: str) -> None:
        catalog = StrictChannelCatalog(*self._catalog(first, second))

        bundle = build_single_model(
            BundleBuilder(charmhub_client=catalog),
            applications={"worker": AppSpec(charm="worker", base="20.04")},
            platform="machine",
        )

        channels = {name: str(app.charm.channel) for name, app in bundle.applications.items()}
        assert channels.pop("worker") == "1.34/stable"
        assert channels
        assert set(channels.values()) == {"1.34/stable"}

    def test_pinned_principal_does_not_follow_a_peer(self) -> None:
        catalog = StrictChannelCatalog(*self._catalog("alpha-cni", "zulu-cni"))

        with pytest.raises(UncompletableBundleError):
            build_single_model(
                BundleBuilder(charmhub_client=catalog),
                applications={"worker": AppSpec(charm="worker", channel="1.33/stable", base="20.04")},
                platform="machine",
            )
