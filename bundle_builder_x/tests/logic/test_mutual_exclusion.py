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

"""Logic tests for mutual exclusion constraints.

Covers Section 3 of charm-deployment-constraints.rst:

A charm may have two or more endpoints that are mutually exclusive - only one
can be active at a time.  This is expressed as a DSL constraint such as:

    len(endpoint[database-legacy]) + len(endpoint[database]) == 1

Real example: canonical-livepatch-server-k8s has a modern ``database``
endpoint and a legacy ``database-legacy`` endpoint; exactly one must be used.
"""

import pytest

from bundle_builder_x.bundle_builder import BundleBuilder, UncompletableBundleError
from bundle_builder_x.charm import CharmEndpoint, EndpointType
from bundle_builder_x.domain import ApplicationConstraint

from .conftest import JUJU, CharmhubClientStub, build_single_model, make_charm


class TestMutualExclusion:
    """Section 3: Mutually exclusive endpoints - exactly one of N must be active."""

    def test_exactly_one_of_two_mutex_endpoints_is_selected(self) -> None:
        # GIVEN a charm with two optional database endpoints and a mutex constraint
        # (modelled after canonical-livepatch-server-k8s)
        livepatch = make_charm(
            "livepatch-k8s",
            endpoints={
                "database": CharmEndpoint(type=EndpointType.REQUIRES, interface="pgsql", optional=True),
                "database-legacy": CharmEndpoint(type=EndpointType.REQUIRES, interface="mysql", optional=True),
            },
            constraint_strs=[
                "len(endpoint[database]) + len(endpoint[database-legacy]) == 1",
            ],
        )
        # AND providers for both alternatives are available
        postgresql = make_charm(
            "postgresql",
            endpoints={
                "database": CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql", optional=True),
            },
        )
        mysql = make_charm(
            "mysql",
            endpoints={
                "database": CharmEndpoint(type=EndpointType.PROVIDES, interface="mysql", optional=True),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(livepatch, postgresql, mysql))

        # WHEN building with only livepatch
        bundle = build_single_model(
            builder,
            applications={"livepatch": ApplicationConstraint(charm="livepatch-k8s")},
            integrations=set(),
            platform="kubernetes",
            arch="amd64",
            juju_version=JUJU,
        )

        # THEN exactly one provider was added (the constraint allows only one)
        charm_names = {a.charm.name for a in bundle.applications.values()}
        providers_added = charm_names & {"postgresql", "mysql"}
        assert len(providers_added) == 1, f"Expected exactly one provider, got {providers_added}"

        # AND exactly one integration exists for livepatch
        livepatch_integrations = [i for i in bundle.integrations if any(ep.application == "livepatch" for ep in i)]
        assert len(livepatch_integrations) == 1

    def test_mutex_constraint_fails_when_no_provider_available(self) -> None:
        # GIVEN the same livepatch charm with the mutex constraint
        livepatch = make_charm(
            "livepatch-k8s",
            endpoints={
                "database": CharmEndpoint(type=EndpointType.REQUIRES, interface="pgsql", optional=True),
                "database-legacy": CharmEndpoint(type=EndpointType.REQUIRES, interface="mysql", optional=True),
            },
            constraint_strs=[
                "len(endpoint[database]) + len(endpoint[database-legacy]) == 1",
            ],
        )
        # AND no providers at all in the registry
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(livepatch))

        # WHEN building
        # THEN it fails - the constraint requires exactly one integration but no provider
        # can be expanded into the domain
        with pytest.raises(UncompletableBundleError):
            build_single_model(
                builder,
                applications={"livepatch": ApplicationConstraint(charm="livepatch-k8s")},
                integrations=set(),
                platform="kubernetes",
                arch="amd64",
                juju_version=JUJU,
            )

    def test_both_mutex_endpoints_simultaneously_violates_constraint(self) -> None:
        # GIVEN the same livepatch charm (constraint: exactly one of the two)
        livepatch = make_charm(
            "livepatch-k8s",
            endpoints={
                "database": CharmEndpoint(type=EndpointType.REQUIRES, interface="pgsql", optional=True),
                "database-legacy": CharmEndpoint(type=EndpointType.REQUIRES, interface="mysql", optional=True),
            },
            constraint_strs=[
                "len(endpoint[database]) + len(endpoint[database-legacy]) == 1",
            ],
        )
        # AND a hypothetical hybrid provider that offers BOTH interfaces
        hybrid = make_charm(
            "hybrid-db",
            endpoints={
                "pgsql-out": CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql", optional=True),
                "mysql-out": CharmEndpoint(type=EndpointType.PROVIDES, interface="mysql", optional=True),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(livepatch, hybrid))

        # WHEN building - the solver will try to satisfy the constraint
        bundle = build_single_model(
            builder,
            applications={"livepatch": ApplicationConstraint(charm="livepatch-k8s")},
            integrations=set(),
            platform="kubernetes",
            arch="amd64",
            juju_version=JUJU,
        )

        # THEN only one of the two endpoints is integrated (== 1, not == 2)
        livepatch_integrations = [i for i in bundle.integrations if any(ep.application == "livepatch" for ep in i)]
        assert len(livepatch_integrations) == 1
