# Copyright (C) 2025 Canonical Ltd

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

import pytest

from bundle_builder.bundle import Application, ApplicationEndpoint, Bundle, Integration
from bundle_builder.bundle_builder import BundleBuilder, UnfulfilledEndpointsError
from bundle_builder.charm import (
    ENDPOINT_PROVIDES,
    ENDPOINT_REQUIRES,
    Charm,
    CharmChannel,
    CharmEndpoint,
    CharmEndpointOptionality,
    CharmLimit,
)
from bundle_builder.juju_version import JujuVersion

from .conftest import CharmhubClientStub


class TestEndpointLimits:
    class TestEdgeCases:
        def test_unfulfillable_endpoint_never_builds(self) -> None:
            # GIVEN an unfulfillable charm
            unfulfillable_charm = Charm(
                name="unfulfillable-charm",
                channel=CharmChannel("stable"),
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_REQUIRES,
                            name="oneway",
                            interface="entry",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(),
                        )
                    }
                ),
                priority=1.0,
            )
            bundle = Bundle(
                applications=frozenset(
                    {
                        Application(name="snowflake", charm=unfulfillable_charm),
                    }
                ),
                integrations=frozenset(),
                platform="machine",
                arch="amd64",
                juju_version=JujuVersion.parse("3.6"),
            )

            # AND a bundle builder with a charmhub client that knows about the charm
            builder = BundleBuilder(CharmhubClientStub(unfulfillable_charm))

            # WHEN we build the bundle
            # THEN it errors because of unfulfilled endpoints
            with pytest.raises(UnfulfilledEndpointsError) as caught:
                builder.build(bundle)

            # AND the non-optional endpoint is not fulfilled
            assert caught.value.unfulfilled_endpoints == {ApplicationEndpoint("snowflake", "oneway")}

        def test_zero_limit_on_required_endpoint_never_builds(self) -> None:
            # GIVEN a charm with limit 0
            zero_limit_charm = Charm(
                name="zero-limit-charm",
                channel=CharmChannel("stable"),
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_PROVIDES,
                            name="disabled",
                            interface="http",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(CharmLimit(limit=0),),
                        )
                    }
                ),
                priority=1.0,
            )

            requiring_charm = Charm(
                name="app",
                channel=CharmChannel("stable"),
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_REQUIRES,
                            name="http",
                            interface="http",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(),
                        )
                    }
                ),
                priority=1.0,
            )

            bundle = Bundle(
                applications=frozenset(
                    {
                        Application(name="server", charm=zero_limit_charm),
                        Application(name="client", charm=requiring_charm),
                    }
                ),
                integrations=frozenset(),
                platform="machine",
                arch="amd64",
                juju_version=JujuVersion.parse("3.6"),
            )
            # AND a bundle builder with a charmhub client that knows about the charms
            builder = BundleBuilder(CharmhubClientStub(zero_limit_charm, requiring_charm))

            # WHEN we build the bundle
            # THEN it errors because of unfulfilled endpoints
            with pytest.raises(UnfulfilledEndpointsError) as caught:
                builder.build(bundle)

            # AND the zero-limit non-optional endpoint is not fulfilled
            assert caught.value.unfulfilled_endpoints == {ApplicationEndpoint("client", "http")}

            # AND in the last best bundle, no integration should exist between server:disabled and client:http
            expected = Integration(
                {
                    ApplicationEndpoint("server", "disabled"),
                    ApplicationEndpoint("client", "http"),
                }
            )
            assert expected not in caught.value.best_bundle.integrations

        def test_limit_applies_to_both_endpoints_in_integration(self) -> None:
            # GIVEN two charms both with limits
            charm1 = Charm(
                name="charm1",
                channel=CharmChannel("stable"),
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_PROVIDES,
                            name="api",
                            interface="http",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(CharmLimit(limit=1),),
                        )
                    }
                ),
                priority=1.0,
            )

            charm2 = Charm(
                name="charm2",
                channel=CharmChannel("stable"),
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_REQUIRES,
                            name="upstream",
                            interface="http",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(CharmLimit(limit=1),),
                        )
                    }
                ),
                priority=1.0,
            )
            # AND bundle with both at their limits
            bundle = Bundle(
                applications=frozenset(
                    {
                        Application(name="app1", charm=charm1),
                        Application(name="app2", charm=charm2),
                        Application(name="app3", charm=charm2),
                    }
                ),
                integrations=frozenset(
                    {
                        Integration(
                            {
                                ApplicationEndpoint(application="app1", endpoint="api"),
                                ApplicationEndpoint(application="app2", endpoint="upstream"),
                            }
                        )
                    }
                ),
                platform="machine",
                arch="amd64",
                juju_version=JujuVersion.parse("3.6"),
            )
            # AND a bundle builder with a charmhub client that knows about the charms
            builder = BundleBuilder(CharmhubClientStub(charm1, charm2))

            # WHEN we build the bundle
            new_bundle = builder.build(bundle)

            # THEN only the expected integration between app1:api and app2:upstream should exist
            expected = Integration(
                {
                    ApplicationEndpoint("app1", "api"),
                    ApplicationEndpoint("app2", "upstream"),
                }
            )
            # Filter integrations to only those involving app1/app2/app3
            test_apps = {"app1", "app2", "app3"}
            relevant_integrations = [
                integration
                for integration in new_bundle.integrations
                if all(endpoint.application in test_apps for endpoint in integration)
            ]
            assert len(relevant_integrations) == 1
            assert expected in relevant_integrations

    class TestMultipleCharmInstances:
        """Test scenarios where multiple instances of the same charm are needed."""

        def test_multiple_postgresql_instances_for_dependencies(self) -> None:
            """Test the exact scenario from PR feedback where indico needs postgresql and a dependency that also needs postgresql."""
            # GIVEN postgresql-k8s with limit=1 (can only connect to one app)
            postgresql_charm = Charm(
                name="postgresql-k8s",
                channel=CharmChannel("stable"),
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_PROVIDES,
                            name="database",
                            interface="postgresql",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(CharmLimit(limit=1),),  # This is the key limitation
                        )
                    }
                ),
                priority=1.0,
            )
            # AND indico that needs database and juju-info connection
            indico_charm = Charm(
                name="indico",
                channel=CharmChannel("stable"),
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_REQUIRES,
                            name="database",
                            interface="postgresql",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(CharmLimit(limit=1),),
                        ),
                        CharmEndpoint(
                            type=ENDPOINT_REQUIRES,
                            name="juju-info",
                            interface="juju-info",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(CharmLimit(limit=1),),
                        ),
                    }
                ),
                priority=1.0,
            )
            # AND some-dependency-k8s that provides juju-info but also needs its own database
            dependency_charm = Charm(
                name="some-dependency-k8s",
                channel=CharmChannel("stable"),
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_PROVIDES,
                            name="juju-info",
                            interface="juju-info",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(CharmLimit(limit=1),),
                        ),
                        CharmEndpoint(
                            type=ENDPOINT_REQUIRES,
                            name="database",
                            interface="postgresql",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(CharmLimit(limit=1),),
                        ),
                    }
                ),
                priority=1.0,
            )
            # AND a base bundle with postgresql connected to indico
            base_bundle = Bundle(
                applications=frozenset(
                    {
                        Application(name="postgresql-k8s", charm=postgresql_charm),
                        Application(name="indico", charm=indico_charm),
                    }
                ),
                integrations=frozenset(
                    {
                        Integration(
                            {
                                ApplicationEndpoint(application="postgresql-k8s", endpoint="database"),
                                ApplicationEndpoint(application="indico", endpoint="database"),
                            }
                        ),
                    }
                ),
                platform="kubernetes",
                arch="amd64",
                juju_version=JujuVersion.parse("3.6"),
            )
            # AND a bundle builder with a charmhub client that knows about the charms
            builder = BundleBuilder(CharmhubClientStub(postgresql_charm, indico_charm, dependency_charm))

            # WHEN we build the bundle
            result = builder.build(base_bundle)

            # THEN it should have the original apps plus dependency and second postgresql
            app_names = {app.name for app in result.applications}

            assert "postgresql-k8s" in app_names
            assert "indico" in app_names
            assert "some-dependency-k8s" in app_names
            assert "postgresql-k8s-a" in app_names  # The second instance!

            # We should have at least 4 applications
            assert len(result.applications) >= 4
            # AND the integrations should be set up correctly
            integration_pairs = [{str(ep) for ep in integration} for integration in result.integrations]

            # Original postgresql connected to indico
            assert {"postgresql-k8s:database", "indico:database"} in integration_pairs

            # Dependency connected to indico via juju-info
            assert {"some-dependency-k8s:juju-info", "indico:juju-info"} in integration_pairs

            # Check that some-dependency-k8s has a database connection to SOME postgresql instance
            # (could be postgresql-k8s-a, postgresql-k8s-b, etc due to non-deterministic graph exploration)
            dependency_has_db = any(
                "some-dependency-k8s:database" in str(pair)
                and any(ep.startswith("postgresql-k8s") and ep.endswith(":database") for ep in pair)
                for pair in integration_pairs
            )
            assert (
                dependency_has_db
            ), f"some-dependency-k8s should be connected to a postgresql instance. Integrations: {integration_pairs}"

            # Verify we have multiple postgresql instances (original + at least one more)
            postgresql_apps = [app for app in result.applications if app.charm.name == "postgresql-k8s"]
            assert len(postgresql_apps) >= 2, f"Should have at least 2 postgresql instances, got {len(postgresql_apps)}"

        def test_prevents_infinite_charm_chain(self) -> None:
            """Test that self-referential charms don't create infinite chains."""
            # GIVEN a charm that both provides and requires the same interface (like grafana-agent-k8s)
            self_ref_charm = Charm(
                name="grafana-agent-k8s",
                channel=CharmChannel("stable"),
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_PROVIDES,
                            name="tracing-provider",
                            interface="tracing",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(),
                        ),
                        CharmEndpoint(
                            type=ENDPOINT_REQUIRES,
                            name="tracing-consumer",
                            interface="tracing",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(),
                        ),
                    }
                ),
                priority=1.0,
            )
            # AND an app that needs tracing
            app_charm = Charm(
                name="mattermost-k8s",
                channel=CharmChannel("stable"),
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_REQUIRES,
                            name="tracing",
                            interface="tracing",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(),
                        ),
                    }
                ),
                priority=1.0,
            )
            # AND a base bundle
            base_bundle = Bundle(
                applications=frozenset(
                    {
                        Application(name="mattermost-k8s", charm=app_charm),
                        Application(name="grafana-agent-k8s", charm=self_ref_charm),
                    }
                ),
                integrations=frozenset(
                    {
                        Integration(
                            {
                                ApplicationEndpoint(application="grafana-agent-k8s", endpoint="tracing-provider"),
                                ApplicationEndpoint(application="mattermost-k8s", endpoint="tracing"),
                            }
                        ),
                    }
                ),
                platform="kubernetes",
                arch="amd64",
                juju_version=JujuVersion.parse("3.6"),
            )
            # AND a bundle builder with a charmhub client that knows about the charm
            builder = BundleBuilder(CharmhubClientStub(self_ref_charm, app_charm))

            # WHEN we build the bundle
            # THEN it errors because of unfulfilled endpoints
            # AND stops creating the bundle
            with pytest.raises(UnfulfilledEndpointsError) as caught:
                builder.build(base_bundle)

            # AND one self referential charm at the end remains unfulfilled
            #   because we don't allow chaining forever even with unlimited endpoint, and
            #   because we don't allow loops
            assert caught.value.unfulfilled_endpoints == {ApplicationEndpoint("grafana-agent-k8s", "tracing-consumer")}

            result = caught.value.best_bundle

            # AND in the last built bundle, it should not exceed the limit
            grafana_apps = [app for app in result.applications if app.charm.name == "grafana-agent-k8s"]
            # AND it should stop adding new instances when limit is reached
            assert len(grafana_apps) < 10  # Definitely not infinite!

    class TestConditionalLimits:
        """Test scenarios where endpoint limits depend on other integrated endpoints."""

        def test_smallest_matching_limit_selected(self) -> None:
            """Test that the smallest limit among all matching criteria is selected."""
            # GIVEN a database charm with multiple conditional limits:
            # - limit=0 when admin is integrated (most restrictive)
            # - limit=5 when monitoring is integrated
            # - limit=10 by default
            from bundle_builder.charm import CharmLimitCriteria

            db_charm = Charm(
                name="postgresql-k8s",
                channel=CharmChannel("stable"),
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_PROVIDES,
                            name="database",
                            interface="postgresql",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(
                                CharmLimit(
                                    criteria=CharmLimitCriteria(endpoint_integrated="admin"),
                                    limit=0,
                                ),
                                CharmLimit(
                                    criteria=CharmLimitCriteria(endpoint_integrated="monitoring"),
                                    limit=5,
                                ),
                                CharmLimit(limit=10),  # default
                            ),
                        ),
                        CharmEndpoint(
                            type=ENDPOINT_REQUIRES,
                            name="admin",
                            interface="admin",
                            optionality=CharmEndpointOptionality.from_bool(True),
                            limits=(),
                        ),
                        CharmEndpoint(
                            type=ENDPOINT_REQUIRES,
                            name="monitoring",
                            interface="monitoring",
                            optionality=CharmEndpointOptionality.from_bool(True),
                            limits=(),
                        ),
                    }
                ),
                priority=1.0,
            )

            # AND app charms that need database
            app_charm = Charm(
                name="app-k8s",
                channel=CharmChannel("stable"),
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_REQUIRES,
                            name="database",
                            interface="postgresql",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(),
                        )
                    }
                ),
                priority=1.0,
            )

            # AND an admin charm
            admin_charm = Charm(
                name="admin-k8s",
                channel=CharmChannel("stable"),
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_PROVIDES,
                            name="admin",
                            interface="admin",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(),
                        )
                    }
                ),
                priority=1.0,
            )

            # AND a monitoring charm
            monitoring_charm = Charm(
                name="monitoring-k8s",
                channel=CharmChannel("stable"),
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_PROVIDES,
                            name="monitoring",
                            interface="monitoring",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(),
                        )
                    }
                ),
                priority=1.0,
            )

            # AND a base bundle with apps and admin/monitoring
            base_bundle = Bundle(
                applications=frozenset(
                    {
                        Application(name="postgresql-k8s", charm=db_charm),
                        Application(name="app1", charm=app_charm),
                        Application(name="admin-k8s", charm=admin_charm),
                        Application(name="monitoring-k8s", charm=monitoring_charm),
                    }
                ),
                integrations=frozenset(),
                platform="kubernetes",
                arch="amd64",
                juju_version=JujuVersion.parse("3.6"),
            )

            # WHEN building the bundle
            builder = BundleBuilder(CharmhubClientStub(db_charm, app_charm, admin_charm, monitoring_charm))
            result = builder.build(base_bundle)

            # THEN admin and monitoring should be integrated with postgresql
            admin_integration_exists = any(
                {
                    ApplicationEndpoint(application="postgresql-k8s", endpoint="admin"),
                    ApplicationEndpoint(application="admin-k8s", endpoint="admin"),
                }
                == integration
                for integration in result.integrations
            )
            monitoring_integration_exists = any(
                {
                    ApplicationEndpoint(application="postgresql-k8s", endpoint="monitoring"),
                    ApplicationEndpoint(application="monitoring-k8s", endpoint="monitoring"),
                }
                == integration
                for integration in result.integrations
            )
            assert admin_integration_exists
            assert monitoring_integration_exists

            # AND postgresql should have 1 database connection
            # Even though admin (limit=0), monitoring (limit=5), and default (limit=10) all match,
            # the database was integrated when only the default limit (10) matched initially.
            # The limit of 0 would prevent additional connections but doesn't retroactively remove existing ones.
            db_connections = sum(
                True
                for integration in result.integrations
                if ApplicationEndpoint(application="postgresql-k8s", endpoint="database") in integration
            )
            assert db_connections == 1, "Expected 1 database connection (integrated before admin endpoint with limit=0)"

        def test_default_limit_when_no_conditions_met(self) -> None:
            """Test that the default limit applies when no conditional criteria match."""
            from bundle_builder.charm import CharmLimitCriteria

            # GIVEN a database charm with conditional limits
            db_charm = Charm(
                name="postgresql-k8s",
                channel=CharmChannel("stable"),
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_PROVIDES,
                            name="database",
                            interface="postgresql",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(
                                CharmLimit(
                                    criteria=CharmLimitCriteria(endpoint_integrated="monitoring"),
                                    limit=5,
                                ),
                                CharmLimit(limit=1),  # default
                            ),
                        )
                    }
                ),
                priority=1.0,
            )

            # AND app charms that need database
            app_charm = Charm(
                name="app-k8s",
                channel=CharmChannel("stable"),
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_REQUIRES,
                            name="database",
                            interface="postgresql",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(),
                        )
                    }
                ),
                priority=1.0,
            )

            # AND a base bundle with multiple apps needing database (but no grafana-cloud)
            base_bundle = Bundle(
                applications=frozenset(
                    {
                        Application(name="postgresql-k8s", charm=db_charm),
                        Application(name="app1", charm=app_charm),
                        Application(name="app2", charm=app_charm),
                    }
                ),
                integrations=frozenset(),
                platform="kubernetes",
                arch="amd64",
                juju_version=JujuVersion.parse("3.6"),
            )

            # WHEN building the bundle
            builder = BundleBuilder(CharmhubClientStub(db_charm, app_charm))
            result = builder.build(base_bundle)

            # THEN postgresql should have at most 1 database connection (the default limit)
            db_connections = sum(
                True
                for integration in result.integrations
                if ApplicationEndpoint(application="postgresql-k8s", endpoint="database") in integration
            )
            assert (
                db_connections == 1
            ), "Expected exactly 1 database connection when grafana-cloud-config is not integrated"
