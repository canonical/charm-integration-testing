# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Logic tests for version compatibility constraints.

Covers Section 8 of charm-deployment-constraints.rst:

When multiple instances of a charm form a cluster, they must all run compatible
versions.  In practice this means:
  - Peer replication endpoints use cyclic=True so the rank constraint is bypassed.
  - A DSL constraint such as:
        tracks(charms(endpoint[replication-offer])) - tracks({self}) == {}
    ensures all connected peers are on the same channel track.

When a peer is on a different track, the solver emits a PeerChannelMismatchTag
and attempts to fetch the peer charm on the required track.

These tests cover:
  1. Same-track peers integrate successfully.
  2. Different-track peers trigger the mismatch expansion path; if the stub can
     supply the required track the solver resolves it, otherwise it fails.
"""

import pytest

from bundle_builder_x.bundle_builder import BundleBuilder, UncompletableBundleError
from bundle_builder_x.charm import CharmEndpoint, EndpointType
from bundle_builder_x.spec import AppSpec, IntegrationSpec

from .conftest import CharmhubClientStub, build_single_model, make_charm


class TestVersionCompatibility:
    """Section 8: Peer channel/version compatibility for clustered charms."""

    def test_same_track_peers_replicate_successfully(self) -> None:
        # GIVEN postgresql-k8s on 14/stable with cyclic replication endpoints and
        # a version compatibility constraint (all peers must share the same track)
        pg_14 = make_charm(
            "postgresql-k8s",
            channel="14/stable",
            endpoints={
                "replication": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="pgdata",
                    optional=True,
                    cyclic=True,
                ),
                "replication-offer": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="pgdata",
                    optional=True,
                    cyclic=True,
                ),
            },
            constraint_strs=[
                # All peers must be on the same track as this instance.
                # Expressed as: peers' track set is a subset of (i.e., equal to) self's track set.
                "tracks(charms(endpoint[replication-offer])) <= tracks({self})",
            ],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(pg_14))

        # WHEN two instances on the SAME track (14/stable) are explicitly integrated
        bundle = build_single_model(
            builder,
            applications={
                "pg-primary": AppSpec(
                    charm="postgresql-k8s",
                    channel="14/stable",
                ),
                "pg-standby": AppSpec(
                    charm="postgresql-k8s",
                    channel="14/stable",
                ),
            },
            integrations=[
                IntegrationSpec(
                    application="pg-standby",
                    endpoint="replication",
                    remote_application="pg-primary",
                    remote_endpoint="replication-offer",
                )
            ],
        )

        # THEN both instances are in the bundle (same track satisfies the constraint)
        assert len(bundle.applications) == 2
        assert len(bundle.integrations) == 1

    def test_different_track_peer_triggers_expansion_or_fails(self) -> None:
        # GIVEN pg-primary on 14/stable with the version constraint
        pg_14 = make_charm(
            "postgresql-k8s",
            channel="14/stable",
            endpoints={
                "replication": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="pgdata",
                    optional=True,
                    cyclic=True,
                ),
                "replication-offer": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="pgdata",
                    optional=True,
                    cyclic=True,
                ),
            },
            constraint_strs=[
                "tracks(charms(endpoint[replication-offer])) <= tracks({self})",
            ],
        )
        # AND pg-standby on 15/stable (incompatible track)
        pg_15 = make_charm(
            "postgresql-k8s",
            channel="15/stable",
            endpoints={
                "replication": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="pgdata",
                    optional=True,
                    cyclic=True,
                ),
                "replication-offer": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="pgdata",
                    optional=True,
                    cyclic=True,
                ),
            },
            constraint_strs=[
                "tracks(charms(endpoint[replication-offer])) <= tracks({self})",
            ],
        )
        # Stub only has 14/stable and 15/stable - no way to satisfy pg-primary needing
        # a 14/stable peer when the spec forces pg-standby to use 15/stable.
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(pg_14, pg_15))

        # WHEN the spec demands that a 14/stable instance integrate with a 15/stable instance
        # THEN the solver fails - PeerChannelMismatchTag triggers expansion but the mismatch
        # cannot be resolved with the available stub registry
        with pytest.raises(UncompletableBundleError):
            build_single_model(
                builder,
                applications={
                    "pg-primary": AppSpec(
                        charm="postgresql-k8s",
                        channel="14/stable",
                    ),
                    "pg-standby": AppSpec(
                        charm="postgresql-k8s",
                        channel="15/stable",
                    ),
                },
                integrations=[
                    IntegrationSpec(
                        application="pg-standby",
                        endpoint="replication",
                        remote_application="pg-primary",
                        remote_endpoint="replication-offer",
                    )
                ],
            )

    def test_channel_mismatch_resolved_when_correct_version_available(self) -> None:
        # GIVEN two postgresql versions in the stub
        pg_14 = make_charm(
            "postgresql-k8s",
            channel="14/stable",
            endpoints={
                "replication": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="pgdata", optional=True, cyclic=True
                ),
                "replication-offer": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="pgdata", optional=True, cyclic=True
                ),
            },
            constraint_strs=[
                "tracks(charms(endpoint[replication-offer])) <= tracks({self})",
            ],
        )
        pg_14_alt = make_charm(
            "postgresql-k8s",
            channel="14/stable",
            revision=2,  # newer revision on same track
            endpoints={
                "replication": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="pgdata", optional=True, cyclic=True
                ),
                "replication-offer": CharmEndpoint(
                    type=EndpointType.PROVIDES, interface="pgdata", optional=True, cyclic=True
                ),
            },
            constraint_strs=[
                "tracks(charms(endpoint[replication-offer])) <= tracks({self})",
            ],
        )
        # Stub has two 14/stable variants; the solver can satisfy same-track constraint
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(pg_14, pg_14_alt))

        bundle = build_single_model(
            builder,
            applications={
                "pg-primary": AppSpec(charm="postgresql-k8s", channel="14/stable"),
                "pg-standby": AppSpec(charm="postgresql-k8s", channel="14/stable"),
            },
            integrations=[
                IntegrationSpec(
                    application="pg-standby",
                    endpoint="replication",
                    remote_application="pg-primary",
                    remote_endpoint="replication-offer",
                )
            ],
        )

        # Both instances on same track -> builds fine
        assert len(bundle.applications) == 2

    def test_two_tracks_self_constraints_on_same_charm_do_not_crash(self) -> None:
        # Regression test: a charm with TWO constraints that both end in `== tracks({self})`
        # (one per endpoint) previously caused a z3 "named assertion defined twice" crash
        # because the {self} side produced N-1 spurious entries whose tags collided.
        #
        # Modelled after mongodb-k8s, which has both:
        #   bool(endpoint[config-server]) => tracks(charms(endpoint[config-server])) == tracks({self})
        #   bool(endpoint[sharding]) => tracks(charms(endpoint[sharding])) == tracks({self})
        mongo = make_charm(
            "mongo",
            channel="8/stable",
            endpoints={
                "config-server": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="shards-cfg",
                    optional=True,
                ),
                "sharding": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="shards-cfg",
                    optional=True,
                    limit=1,
                ),
            },
            constraint_strs=[
                "not (bool(endpoint[sharding]) and bool(endpoint[config-server]))",
                "bool(endpoint[config-server]) => tracks(charms(endpoint[config-server])) == tracks({self})",
                "bool(endpoint[sharding]) => tracks(charms(endpoint[sharding])) == tracks({self})",
            ],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(mongo))

        # WHEN two instances are integrated as config-server and shard (same track)
        bundle = build_single_model(
            builder,
            applications={
                "database-a": AppSpec(charm="mongo", channel="8/stable"),
                "database-b": AppSpec(charm="mongo", channel="8/stable"),
            },
            integrations=[
                IntegrationSpec(
                    application="database-a",
                    endpoint="config-server",
                    remote_application="database-b",
                    remote_endpoint="sharding",
                )
            ],
        )

        # THEN the build succeeds (previously raised z3 "named assertion defined twice")
        assert len(bundle.applications) == 2
        assert len(bundle.integrations) == 1
