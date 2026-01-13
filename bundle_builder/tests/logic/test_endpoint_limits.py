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


from bundle_builder.bundle import Application, ApplicationEndpoint, Bundle, Integration
from bundle_builder.bundle_builder import BundleBuilder
from bundle_builder.charm import (
    ENDPOINT_PROVIDES,
    ENDPOINT_REQUIRES,
    Charm,
    CharmEndpoint,
    CharmEndpointOptionality,
    CharmLimit,
)

from .conftest import CharmhubClientStub


class TestEndpointLimits:
    class TestEdgeCases:
        def test_zero_limit_blocks_all_connections(self) -> None:
            # GIVEN a charm with limit 0
            zero_limit_charm = Charm(
                name="zero-limit-charm",
                channel="stable",
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
                channel="stable",
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
            )
            # AND a bundle builder with a charmhub client that knows about the charms
            builder = BundleBuilder(CharmhubClientStub(zero_limit_charm, requiring_charm))

            # WHEN we build the bundle
            new_bundle = builder.build(bundle)

            # THEN no integration should exist between server:disabled and client:http
            expected = Integration(
                {
                    ApplicationEndpoint("server", "disabled"),
                    ApplicationEndpoint("client", "http"),
                }
            )
            assert expected not in new_bundle.integrations

        def test_limit_applies_to_both_endpoints_in_integration(self) -> None:
            # GIVEN two charms both with limits
            charm1 = Charm(
                name="charm1",
                channel="stable",
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
                channel="stable",
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
                channel="stable",
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
                channel="stable",
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
                channel="stable",
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
                channel="stable",
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
                channel="stable",
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
            )
            # AND a bundle builder with a charmhub client that knows about the charm
            builder = BundleBuilder(CharmhubClientStub(self_ref_charm, app_charm))

            # WHEN we build the bundle
            result = builder.build(base_bundle)

            # THEN it should not exceed the limit
            grafana_apps = [app for app in result.applications if app.charm.name == "grafana-agent-k8s"]
            # AND it should stop adding new instances when limit is reached
            assert len(grafana_apps) < 10  # Definitely not infinite!

    class TestConditionalLimits:
        """Test scenarios where endpoint limits depend on other integrated endpoints."""

        def test_limit_increases_when_condition_met(self):
            """Test that an endpoint's limit can increase when a specific endpoint is integrated."""
            # GIVEN a database charm with conditional limits:
            # - limit=1 by default
            # - limit=10 when grafana-cloud-config is integrated
            from bundle_builder.charm import CharmLimitCriteria

            db_charm = Charm(
                name="postgresql-k8s",
                channel="stable",
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
                                    criteria=CharmLimitCriteria(endpoint_integrated="grafana-cloud-config"),
                                    limit=10,
                                ),
                                CharmLimit(limit=1),
                            ),
                        ),
                        CharmEndpoint(
                            type=ENDPOINT_REQUIRES,
                            name="grafana-cloud-config",
                            interface="grafana_cloud_config",
                            optionality=CharmEndpointOptionality.from_bool(True),
                            limits=(),
                        ),
                    }
                ),
                priority=1.0,
            )

            # AND an app charm that needs database
            app_charm = Charm(
                name="app-k8s",
                channel="stable",
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

            # AND a grafana-cloud charm
            grafana_cloud_charm = Charm(
                name="grafana-cloud-k8s",
                channel="stable",
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_PROVIDES,
                            name="grafana-cloud-config",
                            interface="grafana_cloud_config",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limits=(),
                        )
                    }
                ),
                priority=1.0,
            )

            # AND a base bundle with multiple apps needing database
            base_bundle = Bundle(
                applications=frozenset(
                    {
                        Application(name="postgresql-k8s", charm=db_charm),
                        Application(name="app1", charm=app_charm),
                        Application(name="app2", charm=app_charm),
                        Application(name="grafana-cloud-k8s", charm=grafana_cloud_charm),
                    }
                ),
                integrations=frozenset(),
                platform="kubernetes",
                arch="amd64",
            )

            # WHEN building the bundle
            builder = BundleBuilder(CharmhubClientStub(db_charm, app_charm, grafana_cloud_charm))
            result = builder.build(base_bundle)

            # THEN grafana-cloud should be integrated with postgresql
            grafana_cloud_integration_exists = any(
                {
                    ApplicationEndpoint(application="postgresql-k8s", endpoint="grafana-cloud-config"),
                    ApplicationEndpoint(application="grafana-cloud-k8s", endpoint="grafana-cloud-config"),
                }
                == integration
                for integration in result.integrations
            )
            assert grafana_cloud_integration_exists

            # AND postgresql should have more than 1 database connection (the conditional higher limit applies)
            db_connections = sum(
                1
                for integration in result.integrations
                if ApplicationEndpoint(application="postgresql-k8s", endpoint="database") in integration
            )
            assert (
                db_connections > 1
            ), "Expected more than 1 database connection when grafana-cloud-config is integrated"

        def test_limit_stays_low_when_condition_not_met(self):
            """Test that endpoint limit remains low when conditional endpoint is not available."""
            from bundle_builder.charm import CharmLimitCriteria

            # GIVEN a database charm with conditional limits but no grafana-cloud available
            db_charm = Charm(
                name="postgresql-k8s",
                channel="stable",
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
                                    criteria=CharmLimitCriteria(endpoint_integrated="grafana-cloud-config"),
                                    limit=10,
                                ),
                                CharmLimit(limit=1),
                            ),
                        )
                    }
                ),
                priority=1.0,
            )

            # AND app charms that need database
            app_charm = Charm(
                name="app-k8s",
                channel="stable",
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
            )

            # WHEN building the bundle
            builder = BundleBuilder(CharmhubClientStub(db_charm, app_charm))
            result = builder.build(base_bundle)

            # THEN postgresql should have at most 1 database connection (the default limit)
            db_connections = sum(
                1
                for integration in result.integrations
                if ApplicationEndpoint(application="postgresql-k8s", endpoint="database") in integration
            )
            assert (
                db_connections == 1
            ), "Expected exactly 1 database connection when grafana-cloud-config is not integrated"
