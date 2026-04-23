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
from bundle_builder_x.domain import ApplicationConstraint, IntegrationConstraint

from .conftest import JUJU, CharmhubClientStub, build_single_model, make_charm


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
                applications={"proxy": ApplicationConstraint(charm="pgbouncer-k8s")},
                integrations=set(),
                platform="kubernetes",
                arch="amd64",
                juju_version=JUJU,
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
                    "proxy-a": ApplicationConstraint(charm="pgbouncer-k8s"),
                    "proxy-b": ApplicationConstraint(charm="pgbouncer-k8s"),
                },
                integrations={
                    # proxy-a requires database from proxy-b
                    IntegrationConstraint(
                        application_1="proxy-a",
                        endpoint_1="backend-database",
                        application_2="proxy-b",
                        endpoint_2="database",
                    ),
                    # proxy-b requires database from proxy-a  <-- cycle
                    IntegrationConstraint(
                        application_1="proxy-b",
                        endpoint_1="backend-database",
                        application_2="proxy-a",
                        endpoint_2="database",
                    ),
                },
                platform="kubernetes",
                arch="amd64",
                juju_version=JUJU,
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
            applications={"proxy": ApplicationConstraint(charm="pgbouncer-k8s")},
            integrations=set(),
            platform="kubernetes",
            arch="amd64",
            juju_version=JUJU,
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
                "pg-primary": ApplicationConstraint(charm="postgresql-k8s"),
                "pg-standby": ApplicationConstraint(charm="postgresql-k8s"),
            },
            integrations={
                # pg-standby replicates from pg-primary
                IntegrationConstraint(
                    application_1="pg-standby",
                    endpoint_1="replication",
                    application_2="pg-primary",
                    endpoint_2="replication-offer",
                )
            },
            platform="kubernetes",
            arch="amd64",
            juju_version=JUJU,
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
                    "pg-primary": ApplicationConstraint(charm="postgresql-k8s"),
                    "pg-standby": ApplicationConstraint(charm="postgresql-k8s"),
                },
                integrations={
                    IntegrationConstraint(
                        application_1="pg-standby",
                        endpoint_1="replication",
                        application_2="pg-primary",
                        endpoint_2="replication-offer",
                    ),
                    IntegrationConstraint(
                        application_1="pg-primary",
                        endpoint_1="replication",
                        application_2="pg-standby",
                        endpoint_2="replication-offer",
                    ),
                },
                platform="kubernetes",
                arch="amd64",
                juju_version=JUJU,
            )
