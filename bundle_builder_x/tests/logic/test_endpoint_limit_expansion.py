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

"""Logic tests for expanding to a second instance of the same charm in the same model.

Covers the duplicate-charm-expansion guard in `_add_charm_for_charm_id`: when an
endpoint has a connection `limit` and the existing instance is already at capacity,
a second, independent instance is a legitimate way to serve another consumer, so the
guard must not block it. When the compatible endpoint has no limit, a second instance
is redundant and must still be blocked (otherwise a genuinely unsatisfiable endpoint
would cause unbounded, non-terminating expansion).
"""

import pytest

from bundle_builder_x.bundle_builder import BundleBuilder, UncompletableBundleError
from bundle_builder_x.charm import CharmEndpoint, EndpointType
from bundle_builder_x.spec import AppSpec

from .conftest import CharmhubClientStub, build_single_model, make_charm


class TestEndpointLimitExpansion:
    def test_limited_provider_expands_to_a_second_instance(self) -> None:
        # GIVEN a provider whose "database" endpoint only accepts one consumer (limit=1)
        provider = make_charm(
            "limited-db",
            endpoints={
                "database": CharmEndpoint(type=EndpointType.PROVIDES, interface="db", optional=True, limit=1),
            },
        )
        # AND a consumer charm that requires that interface
        consumer = make_charm(
            "webapp",
            endpoints={"db": CharmEndpoint(type=EndpointType.REQUIRES, interface="db", optional=False)},
        )
        stub = CharmhubClientStub(provider, consumer)
        builder = BundleBuilder(charmhub_client=stub)

        # WHEN two independent applications both require the limited interface
        bundle = build_single_model(
            builder,
            {
                "app1": AppSpec(charm="webapp"),
                "app2": AppSpec(charm="webapp"),
            },
        )

        # THEN the solver adds a second "limited-db" instance to serve the second consumer,
        # rather than reporting the bundle as uncompletable
        db_instances = [a for a in bundle.applications.values() if a.charm.name == "limited-db"]
        assert len(db_instances) == 2

    def test_unlimited_provider_does_not_duplicate(self) -> None:
        # GIVEN a provider whose "database" endpoint has no connection limit
        provider = make_charm(
            "unlimited-db",
            endpoints={
                "database": CharmEndpoint(type=EndpointType.PROVIDES, interface="db", optional=True, limit=None),
            },
        )
        consumer = make_charm(
            "webapp",
            endpoints={"db": CharmEndpoint(type=EndpointType.REQUIRES, interface="db", optional=False)},
        )
        stub = CharmhubClientStub(provider, consumer)
        builder = BundleBuilder(charmhub_client=stub)

        # WHEN two independent applications both require the unlimited interface
        bundle = build_single_model(
            builder,
            {
                "app1": AppSpec(charm="webapp"),
                "app2": AppSpec(charm="webapp"),
            },
        )

        # THEN a single "unlimited-db" instance serves both consumers (no redundant duplicate)
        db_instances = [a for a in bundle.applications.values() if a.charm.name == "unlimited-db"]
        assert len(db_instances) == 1

    def test_genuinely_unsatisfiable_endpoint_fails_fast(self) -> None:
        # GIVEN a charm requiring an interface with no provider anywhere in the registry
        orphan = make_charm(
            "orphan",
            endpoints={"db": CharmEndpoint(type=EndpointType.REQUIRES, interface="no-such-interface", optional=False)},
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(orphan))

        # THEN the build fails outright rather than looping/timing out
        with pytest.raises(UncompletableBundleError):
            build_single_model(builder, {"app": AppSpec(charm="orphan")})
