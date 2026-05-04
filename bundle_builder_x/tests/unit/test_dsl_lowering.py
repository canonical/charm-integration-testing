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

"""Unit tests for the DSL lowering pass (dsl_lowering.py).

Focus: properties of the LoweringResult that are hard to observe at the logic
(solver) level, such as the absence of duplicate sub-assertion tags.
"""

import z3  # type: ignore[import-untyped]

from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointType
from bundle_builder_x.constraints_dsl import parse_constraint
from bundle_builder_x.domain import Domain, DomainCharm, DomainCharmEndpoint, DomainModel, ModelRef
from bundle_builder_x.dsl_lowering import LoweringContext, lower
from bundle_builder_x.juju_version import JujuVersion


def _make_charm(name: str, channel: str, endpoints: dict[str, CharmEndpoint] | None = None) -> Charm:
    track, _, risk = channel.partition("/")
    return Charm(
        name=name,
        channel=CharmChannel(track=track, risk=risk or "stable", branch=""),
        revision=1,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints=endpoints or {},
    )


def _make_domain_charm(charm: Charm, charm_id: int, endpoints: dict[str, CharmEndpoint]) -> DomainCharm:
    return DomainCharm(
        exists=z3.Bool(f"charm_{charm_id}_exists"),
        spec=charm,
        model=ModelRef(name="default"),
        endpoints={
            name: DomainCharmEndpoint(
                count=z3.Int(f"charm_{charm_id}_{name}_count"),
                integrated=z3.Bool(f"charm_{charm_id}_{name}_integrated"),
            )
            for name in endpoints
        },
    )


def _make_two_charm_domain(
    charm_a: Charm,
    charm_b: Charm,
    endpoints_a: dict[str, CharmEndpoint],
    endpoints_b: dict[str, CharmEndpoint],
) -> Domain:
    domain = Domain()
    domain.models[ModelRef(name="default")] = DomainModel(
        arch="amd64",
        platform="kubernetes",
        juju_version=JujuVersion(major=3, minor=6, patch=0),
        ref=ModelRef(name="default"),
    )
    domain.charms.append(_make_domain_charm(charm_a, 0, endpoints_a))
    domain.charms.append(_make_domain_charm(charm_b, 1, endpoints_b))
    return domain


class TestTracksOfSelfIsASingleton:
    """tracks({self}) must produce a _ChannelSet with exactly one entry (self).

    Previously, the lowering iterated over all domain charms for every channel-set
    expression including {self}, producing N-1 spurious entries with provably-False
    conditions.  When two constraints on the same charm both referenced tracks({self}),
    those spurious entries generated duplicate PeerChannelMismatchTag names and the
    solver raised "named assertion defined twice".
    """

    def _make_mongo_like_charm(self, channel: str) -> tuple[Charm, dict[str, CharmEndpoint]]:
        endpoints = {
            "config-server": CharmEndpoint(type=EndpointType.PROVIDES, interface="shards-cfg", optional=True),
            "sharding": CharmEndpoint(type=EndpointType.REQUIRES, interface="shards-cfg", optional=True),
        }
        return _make_charm("mongo", channel, endpoints), endpoints

    def test_tracks_self_sub_assertion_tags_unique_across_two_constraints(self) -> None:
        # GIVEN a charm with two constraints both ending in `== tracks({self})`
        # (the exact pattern in mongodb-k8s.yaml that previously crashed)
        charm_spec, endpoints = self._make_mongo_like_charm("8/stable")
        peer_spec, peer_endpoints = self._make_mongo_like_charm("8/stable")
        domain = _make_two_charm_domain(charm_spec, peer_spec, endpoints, peer_endpoints)

        ctx = LoweringContext(charm_id=0, domain_charm=domain.charms[0], domain=domain)

        constraint_1 = parse_constraint(
            "bool(endpoint[config-server]) => tracks(charms(endpoint[config-server])) == tracks({self})"
        )
        constraint_2 = parse_constraint(
            "bool(endpoint[sharding]) => tracks(charms(endpoint[sharding])) == tracks({self})"
        )

        result_1 = lower(constraint_1, ctx)
        result_2 = lower(constraint_2, ctx)

        all_tags = [sub.tag.encode() for sub in result_1.sub_assertions + result_2.sub_assertions]

        # THEN there are no duplicate tag strings across both constraint lowerings
        assert len(all_tags) == len(
            set(all_tags)
        ), f"Duplicate sub-assertion tags found: {[t for t in all_tags if all_tags.count(t) > 1]}"

    def test_tracks_self_produces_no_spurious_peer_entries(self) -> None:
        # GIVEN a domain with 3 charms (self at id=0, two peers)
        charm_spec, endpoints = self._make_mongo_like_charm("8/stable")
        peer_a, ep_a = self._make_mongo_like_charm("8/stable")
        peer_b, ep_b = self._make_mongo_like_charm("7/stable")

        domain = _make_two_charm_domain(charm_spec, peer_a, endpoints, ep_a)
        # Add a third charm manually
        domain.charms.append(_make_domain_charm(peer_b, 2, ep_b))

        ctx = LoweringContext(charm_id=0, domain_charm=domain.charms[0], domain=domain)

        # A constraint that compares tracks of sharding peers against self's track.
        # With the fix, lowering tracks({self}) should only produce one entry (charm_id=0),
        # so the only sub-assertion emitted is for peer charm(s) that differ from self.
        constraint = parse_constraint(
            "bool(endpoint[sharding]) => tracks(charms(endpoint[sharding])) == tracks({self})"
        )
        result = lower(constraint, ctx)

        # Only charm_id=2 (track "7") differs from self (track "8").
        # Charm_id=1 matches self's track, so no sub-assertion for it.
        # Charm_id=0 is self, never blocked.
        # The spurious extra entry for charm_id=1 coming from the {self} side must not appear.
        tags = [sub.tag.encode() for sub in result.sub_assertions]
        assert len(tags) == len(set(tags)), f"Duplicate tags: {tags}"

    def test_solver_does_not_raise_on_two_tracks_self_constraints(self) -> None:
        # Regression test: solver.assert_and_track must not raise "named assertion defined twice"
        # when a charm has two constraints both using tracks({self}).
        charm_spec, endpoints = self._make_mongo_like_charm("8/stable")
        peer_spec, peer_endpoints = self._make_mongo_like_charm("8/stable")
        domain = _make_two_charm_domain(charm_spec, peer_spec, endpoints, peer_endpoints)

        ctx = LoweringContext(charm_id=0, domain_charm=domain.charms[0], domain=domain)

        constraint_1 = parse_constraint(
            "bool(endpoint[config-server]) => tracks(charms(endpoint[config-server])) == tracks({self})"
        )
        constraint_2 = parse_constraint(
            "bool(endpoint[sharding]) => tracks(charms(endpoint[sharding])) == tracks({self})"
        )

        result_1 = lower(constraint_1, ctx)
        result_2 = lower(constraint_2, ctx)

        charm = domain.charms[0]
        solver = z3.Solver()
        solver.set("unsat_core", True)

        # This must not raise z3.Z3Exception("named assertion defined twice")
        solver.assert_and_track(z3.Implies(charm.exists, result_1.expr), "constraint_0")
        solver.assert_and_track(z3.Implies(charm.exists, result_2.expr), "constraint_1")
        for i, sub in enumerate(result_1.sub_assertions + result_2.sub_assertions):
            solver.assert_and_track(z3.Implies(charm.exists, sub.expr), sub.tag.encode())
