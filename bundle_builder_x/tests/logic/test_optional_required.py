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

"""Logic tests for optional vs required endpoints and integration limits.

Covers constraint types from the deployment-constraints reference:
- Section 1: Optional vs Required Integrations
- Section 2: Integration Limits
"""

import pytest

from bundle_builder_x.bundle_builder import BundleBuilder, UncompletableBundleError
from bundle_builder_x.charm import CharmEndpoint, EndpointType
from bundle_builder_x.spec import AppSpec, IntegrationSpec

from .conftest import CharmhubClientStub, build_single_model, make_charm


class TestRequiredEndpoints:
    """Section 1.1: Always-required endpoints must be satisfied by a provider."""

    def test_required_endpoint_causes_expansion(self) -> None:
        # GIVEN a main app charm with a non-optional requires endpoint
        app = make_charm(
            "my-app",
            endpoints={
                "database": CharmEndpoint(type=EndpointType.REQUIRES, interface="pgsql", optional=False),
            },
        )
        # AND a database provider charm in the registry
        pg = make_charm(
            "postgresql",
            endpoints={
                "database": CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql", optional=True),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(app, pg))

        # WHEN building a bundle with only the main app
        bundle = build_single_model(
            builder,
            applications={"app": AppSpec(charm="my-app")},
        )

        # THEN the solver expands the domain and adds the provider
        charm_names = {a.charm.name for a in bundle.applications.values()}
        assert "postgresql" in charm_names

        # AND an integration between them exists
        assert any(
            {"app", "postgresql"} == {ep.application for ep in integration} for integration in bundle.integrations
        )

    def test_required_endpoint_with_no_provider_raises(self) -> None:
        # GIVEN a charm with a non-optional requires endpoint
        app = make_charm(
            "my-app",
            endpoints={
                "database": CharmEndpoint(type=EndpointType.REQUIRES, interface="pgsql", optional=False),
            },
        )
        # AND no provider exists in the registry
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(app))

        # WHEN building
        # THEN it fails because the endpoint cannot be satisfied
        with pytest.raises(UncompletableBundleError):
            build_single_model(
                builder,
                applications={"app": AppSpec(charm="my-app")},
            )

    def test_optional_endpoint_not_expanded_when_no_provider(self) -> None:
        # GIVEN a charm with only optional endpoints
        app = make_charm(
            "my-app",
            endpoints={
                "metrics": CharmEndpoint(type=EndpointType.REQUIRES, interface="prometheus_scrape", optional=True),
            },
        )
        # AND no provider in the registry
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(app))

        # WHEN building
        bundle = build_single_model(
            builder,
            applications={"app": AppSpec(charm="my-app")},
        )

        # THEN it succeeds with just the one charm, no extra integrations
        assert len(bundle.applications) == 1
        assert "app" in bundle.applications
        assert len(bundle.integrations) == 0

    def test_provider_non_optional_endpoint_satisfied_by_requirer(self) -> None:
        # GIVEN a provider charm whose endpoint is non-optional
        # (e.g., a database that must serve at least one consumer)
        db = make_charm(
            "database",
            endpoints={
                "db": CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql", optional=False),
            },
        )
        # AND a consumer charm in the registry
        app = make_charm(
            "app",
            endpoints={
                "db": CharmEndpoint(type=EndpointType.REQUIRES, interface="pgsql", optional=True),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(db, app))

        # WHEN building with only the provider
        bundle = build_single_model(
            builder,
            applications={"db": AppSpec(charm="database")},
        )

        # THEN the solver adds the consumer to satisfy the provider's non-optional endpoint
        charm_names = {a.charm.name for a in bundle.applications.values()}
        assert "app" in charm_names


class TestIntegrationLimits:
    """Section 2: Endpoint integration limits."""

    def test_single_integration_limit_succeeds_with_one_consumer(self) -> None:
        # GIVEN a provider with limit=1 on its endpoint
        # (e.g. content-cache-k8s:nginx-proxy can only serve one upstream)
        proxy = make_charm(
            "content-cache-k8s",
            endpoints={
                "nginx-proxy": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="nginx_route",
                    optional=False,
                    limit=1,
                ),
            },
        )
        # AND exactly one consumer
        webapp = make_charm(
            "webapp",
            endpoints={
                "nginx-route": CharmEndpoint(type=EndpointType.REQUIRES, interface="nginx_route", optional=False),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(proxy, webapp))

        # WHEN building with one consumer explicitly integrated to the proxy
        bundle = build_single_model(
            builder,
            applications={
                "proxy": AppSpec(charm="content-cache-k8s"),
                "web": AppSpec(charm="webapp"),
            },
            integrations=[
                IntegrationSpec(
                    application="proxy",
                    endpoint="nginx-proxy",
                    remote_application="web",
                    remote_endpoint="nginx-route",
                )
            ],
        )

        # THEN the bundle is valid - one integration, limit respected
        assert len(bundle.applications) == 2
        assert len(bundle.integrations) == 1

    def test_single_integration_limit_violated_by_two_consumers_raises(self) -> None:
        # GIVEN a provider with limit=1 on its endpoint
        proxy = make_charm(
            "content-cache-k8s",
            endpoints={
                "nginx-proxy": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="nginx_route",
                    optional=True,
                    limit=1,
                ),
            },
        )
        webapp = make_charm(
            "webapp",
            endpoints={
                "nginx-route": CharmEndpoint(type=EndpointType.REQUIRES, interface="nginx_route", optional=False),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(proxy, webapp))

        # WHEN the spec demands TWO consumers both integrated to the SAME proxy instance
        # (limit=1 means only one integration is allowed on that endpoint)
        with pytest.raises(UncompletableBundleError):
            build_single_model(
                builder,
                applications={
                    "proxy": AppSpec(charm="content-cache-k8s"),
                    "web-a": AppSpec(charm="webapp"),
                    "web-b": AppSpec(charm="webapp"),
                },
                integrations=[
                    IntegrationSpec(
                        application="proxy",
                        endpoint="nginx-proxy",
                        remote_application="web-a",
                        remote_endpoint="nginx-route",
                    ),
                    IntegrationSpec(
                        application="proxy",
                        endpoint="nginx-proxy",
                        remote_application="web-b",
                        remote_endpoint="nginx-route",
                    ),
                ],
            )

    def test_unlimited_endpoint_allows_multiple_consumers(self) -> None:
        # GIVEN a certificates provider with no limit
        # (self-signed-certificates can provide to any number of consumers)
        ssc = make_charm(
            "self-signed-certificates",
            endpoints={
                "certificates": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="tls_certificates",
                    optional=True,
                    limit=None,
                ),
            },
        )
        consumer = make_charm(
            "app",
            endpoints={
                "certificates": CharmEndpoint(
                    type=EndpointType.REQUIRES,
                    interface="tls_certificates",
                    optional=True,
                ),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(ssc, consumer))

        # WHEN three consumers all integrate with the same SSC instance
        bundle = build_single_model(
            builder,
            applications={
                "ssc": AppSpec(charm="self-signed-certificates"),
                "app-a": AppSpec(charm="app"),
                "app-b": AppSpec(charm="app"),
                "app-c": AppSpec(charm="app"),
            },
            integrations=[
                IntegrationSpec(
                    application="ssc",
                    endpoint="certificates",
                    remote_application="app-a",
                    remote_endpoint="certificates",
                ),
                IntegrationSpec(
                    application="ssc",
                    endpoint="certificates",
                    remote_application="app-b",
                    remote_endpoint="certificates",
                ),
                IntegrationSpec(
                    application="ssc",
                    endpoint="certificates",
                    remote_application="app-c",
                    remote_endpoint="certificates",
                ),
            ],
        )

        # THEN the bundle is valid with all three integrations
        assert len(bundle.applications) == 4
        assert len(bundle.integrations) == 3
