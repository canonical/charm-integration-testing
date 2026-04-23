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

"""Logic tests for capability requirements (feature-based endpoint selection).

Covers Section 5 of charm-deployment-constraints.rst:

When multiple charms provide the same interface, a requires charm may need to
distinguish between them based on declared endpoint features.  For example,
temporal-k8s needs ``temporal-admin-k8s`` specifically for its ``admin``
endpoint (not ``temporal-ui-k8s``, even though both provide the same interface).

The mechanism: a requires endpoint declares a set of features it must negotiate,
and a DSL constraint ensures the correct feature set is active when the endpoint
is integrated.

    bool(endpoint[admin]) => features(endpoint[admin]) == {"admin"}
"""

import pytest

from bundle_builder_x.bundle_builder import BundleBuilder, UncompletableBundleError
from bundle_builder_x.charm import CharmEndpoint, EndpointType
from bundle_builder_x.domain import ApplicationConstraint

from .conftest import JUJU, CharmhubClientStub, build_single_model, make_charm


class TestCapabilityRequirements:
    """Section 5: Feature-constrained endpoint selection."""

    def test_feature_constraint_selects_correct_provider(self) -> None:
        # GIVEN temporal-k8s with an admin endpoint that requires the "admin" feature
        # DSL constraint: when admin is connected, features must equal {"admin"}
        temporal = make_charm(
            "temporal-k8s",
            endpoints={
                "admin": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="temporal",
                    optional=False,
                    features=frozenset({"admin"}),
                ),
                "ui": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="temporal",
                    optional=True,
                    features=frozenset({"ui"}),
                ),
            },
            constraint_strs=[
                'bool(endpoint[admin]) => features(endpoint[admin]) == {"admin"}',
                'bool(endpoint[ui]) => features(endpoint[ui]) == {"ui"}',
            ],
        )
        # AND temporal-admin-k8s: provides temporal interface WITH the "admin" feature
        temporal_admin = make_charm(
            "temporal-admin-k8s",
            endpoints={
                "temporal": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="temporal",
                    optional=True,
                    features=frozenset({"admin"}),
                ),
            },
        )
        # AND temporal-ui-k8s: provides temporal interface WITH the "ui" feature only
        temporal_ui = make_charm(
            "temporal-ui-k8s",
            endpoints={
                "temporal": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="temporal",
                    optional=True,
                    features=frozenset({"ui"}),
                ),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(temporal, temporal_admin, temporal_ui))

        # WHEN building with just temporal-k8s (admin endpoint is non-optional)
        bundle = build_single_model(
            builder,
            applications={"temporal": ApplicationConstraint(charm="temporal-k8s")},
            integrations=set(),
            platform="kubernetes",
            arch="amd64",
            juju_version=JUJU,
        )

        # THEN temporal-admin-k8s is added (it has the "admin" feature)
        charm_names = {a.charm.name for a in bundle.applications.values()}
        assert (
            "temporal-admin-k8s" in charm_names
        ), "temporal-admin-k8s should be selected to satisfy the admin feature requirement"

        # AND temporal-ui-k8s is NOT added for the admin endpoint
        # (it may be present if the solver also satisfies the optional ui endpoint,
        # but the admin integration must go to temporal-admin-k8s)
        admin_integration = next(
            (i for i in bundle.integrations if any(ep.endpoint == "admin" for ep in i)),
            None,
        )
        assert admin_integration is not None, "An admin integration should exist"
        admin_provider_app = next(ep.application for ep in admin_integration if ep.endpoint != "admin")
        assert (
            bundle.applications[admin_provider_app].charm.name == "temporal-admin-k8s"
        ), "The admin endpoint must integrate with temporal-admin-k8s, not temporal-ui-k8s"

    def test_wrong_provider_violates_feature_constraint(self) -> None:
        # GIVEN temporal-k8s requiring admin feature
        temporal = make_charm(
            "temporal-k8s",
            endpoints={
                "admin": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="temporal",
                    optional=False,
                    features=frozenset({"admin"}),
                ),
            },
            constraint_strs=[
                'bool(endpoint[admin]) => features(endpoint[admin]) == {"admin"}',
            ],
        )
        # AND ONLY temporal-ui-k8s available (has "ui" feature, not "admin")
        temporal_ui = make_charm(
            "temporal-ui-k8s",
            endpoints={
                "temporal": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="temporal",
                    optional=True,
                    features=frozenset({"ui"}),
                ),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(temporal, temporal_ui))

        # WHEN building - temporal-ui-k8s violates the feature constraint
        # THEN the solver cannot find a valid solution
        with pytest.raises(UncompletableBundleError):
            build_single_model(
                builder,
                applications={"temporal": ApplicationConstraint(charm="temporal-k8s")},
                integrations=set(),
                platform="kubernetes",
                arch="amd64",
                juju_version=JUJU,
            )

    def test_feature_constraint_without_features_accepts_any_provider(self) -> None:
        # GIVEN a charm with a requires endpoint that declares NO feature requirement
        # (plain interface matching - any provider is acceptable)
        app = make_charm(
            "my-app",
            endpoints={
                "db": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="pgsql",
                    optional=False,
                )
            },
        )
        # AND two providers, neither with any feature declarations
        pg_a = make_charm(
            "postgresql-a",
            endpoints={
                "database": CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql", optional=True),
            },
        )
        pg_b = make_charm(
            "postgresql-b",
            endpoints={
                "database": CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql", optional=True),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(app, pg_a, pg_b))

        # WHEN building - either provider is acceptable
        bundle = build_single_model(
            builder,
            applications={"app": ApplicationConstraint(charm="my-app")},
            integrations=set(),
            platform="kubernetes",
            arch="amd64",
            juju_version=JUJU,
        )

        # THEN one of the two providers is selected (no feature constraint to discriminate)
        charm_names = {a.charm.name for a in bundle.applications.values()}
        providers_added = charm_names & {"postgresql-a", "postgresql-b"}
        assert len(providers_added) >= 1
