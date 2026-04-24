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

"""Logic tests for acyclic integration constraints.

Covers Section 6 of charm-deployment-constraints.rst:

The integration graph must not contain dependency cycles.  The solver enforces
this via a rank-based topological ordering: for every integration, the requirer
must have a strictly higher rank than the provider.

A cycle means two integrations simultaneously require A > B and B > A, which is
unsatisfiable.

The ``cyclic=True`` flag on an endpoint explicitly opts it out of the rank
constraint, allowing intentional peer-to-peer integrations such as database
replication clusters.
"""

import pytest

from bundle_builder_x.bundle_builder import BundleBuilder, UncompletableBundleError
from bundle_builder_x.charm import CharmEndpoint, EndpointType
from bundle_builder_x.spec import AppSpec, IntegrationSpec, ModelSpec

from .conftest import JUJU_VERSION, CharmhubClientStub, build_multi_model, build_single_model, make_charm


class TestAcyclicConstraints:
    """Section 6: Cycle detection and cyclic endpoint exemptions."""

    def test_same_charm_cannot_be_its_own_backend(self) -> None:
        # GIVEN pgbouncer-k8s that provides a database interface AND requires one
        # (as a connection pooler, it faces both clients and a real database)
        pgbouncer = make_charm(
            "pgbouncer-k8s",
            endpoints={
                "database": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="postgresql_client",
                    optional=True,
                ),
                "backend-database": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="postgresql_client",
                    optional=False,  # pgbouncer must connect to a real database
                ),
            },
        )
        # AND only pgbouncer itself in the registry (no real postgresql)
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(pgbouncer))

        # WHEN building with one pgbouncer instance
        # THEN it fails: pgbouncer cannot be its own backend (would form a dependency cycle)
        with pytest.raises(UncompletableBundleError):
            build_single_model(
                builder,
                applications={"proxy": AppSpec(charm="pgbouncer-k8s")},
            )

    def test_two_pgbouncers_chained_in_cycle_is_rejected(self) -> None:
        # GIVEN two pgbouncer instances both in the spec and explicitly integrated
        # in a cycle: proxy-a provides database to proxy-b, and proxy-b provides
        # database to proxy-a (both must have their backend-database filled)
        pgbouncer = make_charm(
            "pgbouncer-k8s",
            endpoints={
                "database": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="postgresql_client",
                    optional=False,
                ),
                "backend-database": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="postgresql_client",
                    optional=False,
                ),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(pgbouncer))

        # WHEN the spec demands both integrations (forming a cycle)
        with pytest.raises(UncompletableBundleError):
            build_single_model(
                builder,
                applications={
                    "proxy-a": AppSpec(charm="pgbouncer-k8s"),
                    "proxy-b": AppSpec(charm="pgbouncer-k8s"),
                },
                integrations=[
                    # proxy-a requires database from proxy-b
                    IntegrationSpec(
                        application="proxy-a",
                        endpoint="backend-database",
                        remote_application="proxy-b",
                        remote_endpoint="database",
                    ),
                    # proxy-b requires database from proxy-a  <-- cycle
                    IntegrationSpec(
                        application="proxy-b",
                        endpoint="backend-database",
                        remote_application="proxy-a",
                        remote_endpoint="database",
                    ),
                ],
            )

    def test_acyclic_chain_is_valid(self) -> None:
        # GIVEN the same pgbouncer charm
        pgbouncer = make_charm(
            "pgbouncer-k8s",
            endpoints={
                "database": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="postgresql_client",
                    optional=True,
                ),
                "backend-database": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="postgresql_client",
                    optional=False,
                ),
            },
        )
        # AND a real PostgreSQL provider
        postgresql = make_charm(
            "postgresql-k8s",
            endpoints={
                "database": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="postgresql_client",
                    optional=True,
                ),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(pgbouncer, postgresql))

        # WHEN building with proxy -> postgresql (no cycle - a linear chain)
        bundle = build_single_model(
            builder,
            applications={"proxy": AppSpec(charm="pgbouncer-k8s")},
        )

        # THEN the solver adds postgresql as the backend (no cycle, chain is valid)
        charm_names = {a.charm.name for a in bundle.applications.values()}
        assert "postgresql-k8s" in charm_names

    def test_cyclic_endpoints_allow_peer_integration(self) -> None:
        # GIVEN postgresql-k8s with a replication endpoint marked cyclic=True
        # (peer replication between two instances is an intentional cycle)
        postgresql = make_charm(
            "postgresql-k8s",
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
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(postgresql))

        # WHEN two postgresql instances are explicitly integrated for replication
        bundle = build_single_model(
            builder,
            applications={
                "pg-primary": AppSpec(charm="postgresql-k8s"),
                "pg-standby": AppSpec(charm="postgresql-k8s"),
            },
            integrations=[
                # pg-standby replicates from pg-primary
                IntegrationSpec(
                    application="pg-standby",
                    endpoint="replication",
                    remote_application="pg-primary",
                    remote_endpoint="replication-offer",
                )
            ],
        )

        # THEN both instances are in the bundle (cyclic=True bypasses the rank check)
        assert len(bundle.applications) == 2
        charm_names = {a.charm.name for a in bundle.applications.values()}
        assert charm_names == {"postgresql-k8s"}

        # AND the replication integration is present
        assert len(bundle.integrations) == 1

    def test_non_cyclic_peer_integration_is_rejected(self) -> None:
        # GIVEN the same replication setup but WITHOUT cyclic=True
        postgresql_non_cyclic = make_charm(
            "postgresql-k8s",
            endpoints={
                "replication": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="pgdata",
                    optional=False,  # must be connected
                    cyclic=False,  # no cyclic exemption
                ),
                "replication-offer": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="pgdata",
                    optional=False,  # must be connected
                    cyclic=False,
                ),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(postgresql_non_cyclic))

        # WHEN both instances must integrate with each other (and with themselves via the
        # opposite replication endpoint) - this creates a rank ordering contradiction
        with pytest.raises(UncompletableBundleError):
            build_single_model(
                builder,
                applications={
                    "pg-primary": AppSpec(charm="postgresql-k8s"),
                    "pg-standby": AppSpec(charm="postgresql-k8s"),
                },
                integrations=[
                    IntegrationSpec(
                        application="pg-standby",
                        endpoint="replication",
                        remote_application="pg-primary",
                        remote_endpoint="replication-offer",
                    ),
                    IntegrationSpec(
                        application="pg-primary",
                        endpoint="replication",
                        remote_application="pg-standby",
                        remote_endpoint="replication-offer",
                    ),
                ],
            )


class TestCrossModelAcyclicConstraints:
    """Section 6 (cross-model): user-specified CMR direction is preserved; explicit
    bidirectional CMRs between two models form a rank cycle and are rejected.

    When a charm exposes both sides of an interface (e.g. vault can both provide
    and require vault-autounseal), the solver must not freely activate the reverse
    cross-model integration if the user has already pinned one direction via a user CMR.
    """

    def test_user_specified_cmr_direction_is_not_reversed(self) -> None:
        # GIVEN a charm that exposes both sides of the same interface
        # (like vault, which can both seal and be sealed via vault-autounseal)
        symmetric = make_charm(
            "vault-like",
            endpoints={
                "my-provides": CharmEndpoint(type=EndpointType.PROVIDES, interface="vault-autounseal", optional=True),
                "my-requires": CharmEndpoint(type=EndpointType.REQUIRES, interface="vault-autounseal", optional=True),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(symmetric))

        # WHEN the spec pins only one direction: model-a.a requires from model-b.b
        solution = build_multi_model(
            builder,
            [
                ModelSpec(
                    name="model-a",
                    applications={"a": AppSpec(charm="vault-like")},
                    integrations=[
                        IntegrationSpec(
                            application="a",
                            endpoint="my-requires",
                            remote_model="model-b",
                            remote_application="b",
                            remote_endpoint="my-provides",
                            offer_name="vault-offer",
                            url="ctrl:admin/model-b.vault-offer",
                        ),
                    ],
                    juju=JUJU_VERSION,
                ),
                ModelSpec(
                    name="model-b",
                    applications={"b": AppSpec(charm="vault-like")},
                    juju=JUJU_VERSION,
                ),
            ],
        )

        # THEN model-a's bundle contains exactly the user-specified CMR (requires side)
        bundle_a = next(b for b in solution.bundles if b.model == "model-a")
        requires_cmrs = [c for c in bundle_a.cross_model_integrations if c.local.endpoint == "my-requires"]
        assert len(requires_cmrs) == 1

        # AND no reverse CMR (a:my-provides -> b:my-requires) is present
        reverse_cmrs = [
            c
            for c in bundle_a.cross_model_integrations
            if c.local.endpoint == "my-provides" and c.remote_model == "model-b"
        ]
        assert len(reverse_cmrs) == 0

    def test_both_cmr_directions_simultaneously_is_rejected(self) -> None:
        # GIVEN the same symmetric charm
        symmetric = make_charm(
            "vault-like",
            endpoints={
                "my-provides": CharmEndpoint(type=EndpointType.PROVIDES, interface="vault-autounseal", optional=True),
                "my-requires": CharmEndpoint(type=EndpointType.REQUIRES, interface="vault-autounseal", optional=True),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(symmetric))

        # WHEN both directions are explicitly specified as user CMRs
        # (a requires from b, AND b requires from a simultaneously)
        # THEN the solver rejects this as a rank cycle
        with pytest.raises(UncompletableBundleError):
            build_multi_model(
                builder,
                [
                    ModelSpec(
                        name="model-a",
                        applications={"a": AppSpec(charm="vault-like")},
                        integrations=[
                            IntegrationSpec(
                                application="a",
                                endpoint="my-requires",
                                remote_model="model-b",
                                remote_application="b",
                                remote_endpoint="my-provides",
                                offer_name="vault-offer",
                                url="ctrl:admin/model-b.vault-offer",
                            )
                        ],
                        juju=JUJU_VERSION,
                    ),
                    ModelSpec(
                        name="model-b",
                        applications={"b": AppSpec(charm="vault-like")},
                        integrations=[
                            IntegrationSpec(
                                application="b",
                                endpoint="my-requires",
                                remote_model="model-a",
                                remote_application="a",
                                remote_endpoint="my-provides",
                                offer_name="vault-offer-reverse",
                                url="ctrl:admin/model-a.vault-offer-reverse",
                            )
                        ],
                        juju=JUJU_VERSION,
                    ),
                ],
            )
