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
)
from bundle_builder.charmhub import CharmhubClient


class TestEndpointLimits:
    class TestBundleBuilderLimitValidation:
        def test_can_add_integration_respects_limits(self):
            # GIVEN a charm with limited endpoint
            limited_charm = Charm(
                name="limited-charm",
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
                            limit=1,
                        )
                    }
                ),
            )

            # AND a charm that requires database
            requiring_charm1 = Charm(
                name="app1",
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
                            limit=None,
                        )
                    }
                ),
            )

            requiring_charm2 = Charm(
                name="app2",
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
                            limit=None,
                        )
                    }
                ),
            )

            # AND a bundle with these applications
            bundle = Bundle(
                applications=frozenset(
                    {
                        Application(name="db", charm=limited_charm),
                        Application(name="app1", charm=requiring_charm1),
                        Application(name="app2", charm=requiring_charm2),
                    }
                ),
                integrations=frozenset(
                    {
                        Integration(
                            {
                                ApplicationEndpoint(application="db", endpoint="database"),
                                ApplicationEndpoint(application="app1", endpoint="database"),
                            }
                        )
                    }
                ),
                platform="machine",
                arch="amd64",
            )

            # WHEN checking if we can add another integration
            builder = BundleBuilder(CharmhubClient())
            can_add = builder._can_add_integration_within_charm_limits(
                bundle,
                ApplicationEndpoint(application="db", endpoint="database"),
                ApplicationEndpoint(application="app2", endpoint="database"),
            )

            # THEN it should return False (limit reached)
            assert can_add is False

        def test_can_add_integration_allows_when_under_limit(self):
            # GIVEN a charm with higher limit
            limited_charm = Charm(
                name="limited-charm",
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
                            limit=2,
                        )
                    }
                ),
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
                            name="database",
                            interface="postgresql",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limit=None,
                        )
                    }
                ),
            )

            # AND a bundle with one existing integration
            bundle = Bundle(
                applications=frozenset(
                    {
                        Application(name="db", charm=limited_charm),
                        Application(name="app1", charm=requiring_charm),
                        Application(name="app2", charm=requiring_charm),
                    }
                ),
                integrations=frozenset(
                    {
                        Integration(
                            {
                                ApplicationEndpoint(application="db", endpoint="database"),
                                ApplicationEndpoint(application="app1", endpoint="database"),
                            }
                        )
                    }
                ),
                platform="machine",
                arch="amd64",
            )

            # WHEN checking if we can add another integration
            builder = BundleBuilder(CharmhubClient())
            can_add = builder._can_add_integration_within_charm_limits(
                bundle,
                ApplicationEndpoint(application="db", endpoint="database"),
                ApplicationEndpoint(application="app2", endpoint="database"),
            )

            # THEN it should return True (under limit)
            assert can_add is True

        def test_can_add_integration_allows_unlimited_endpoints(self):
            # GIVEN charms with no limits
            unlimited_charm = Charm(
                name="unlimited-charm",
                channel="stable",
                revision=1,
                ubuntu_version="22.04",
                ubuntu_arch="amd64",
                endpoints=frozenset(
                    {
                        CharmEndpoint(
                            type=ENDPOINT_PROVIDES,
                            name="http",
                            interface="http",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limit=None,
                        )
                    }
                ),
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
                            limit=None,
                        )
                    }
                ),
            )

            # AND a bundle with existing integrations
            bundle = Bundle(
                applications=frozenset(
                    {
                        Application(name="server", charm=unlimited_charm),
                        Application(name="client1", charm=requiring_charm),
                        Application(name="client2", charm=requiring_charm),
                    }
                ),
                integrations=frozenset(
                    {
                        Integration(
                            {
                                ApplicationEndpoint(application="server", endpoint="http"),
                                ApplicationEndpoint(application="client1", endpoint="http"),
                            }
                        )
                    }
                ),
                platform="machine",
                arch="amd64",
            )

            # WHEN checking if we can add another integration
            builder = BundleBuilder(CharmhubClient())
            can_add = builder._can_add_integration_within_charm_limits(
                bundle,
                ApplicationEndpoint(application="server", endpoint="http"),
                ApplicationEndpoint(application="client2", endpoint="http"),
            )

            # THEN it should return True (no limit)
            assert can_add is True

    class TestBundleUnfulfilledEndpoints:
        def test_unfulfilled_endpoints_considers_limits(self):
            # GIVEN a charm with limit 1
            limited_charm = Charm(
                name="limited-charm",
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
                            limit=1,
                        )
                    }
                ),
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
                            name="database",
                            interface="postgresql",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limit=None,
                        )
                    }
                ),
            )

            # AND a bundle where the limit is reached
            bundle = Bundle(
                applications=frozenset(
                    {Application(name="db", charm=limited_charm), Application(name="app", charm=requiring_charm)}
                ),
                integrations=frozenset(
                    {
                        Integration(
                            {
                                ApplicationEndpoint(application="db", endpoint="database"),
                                ApplicationEndpoint(application="app", endpoint="database"),
                            }
                        )
                    }
                ),
                platform="machine",
                arch="amd64",
            )

            # WHEN getting unfulfilled endpoints
            unfulfilled = bundle.unfulfilled_endpoints

            # THEN the limited endpoint should not be unfulfilled (limit reached)
            db_endpoint = ApplicationEndpoint(application="db", endpoint="database")
            assert db_endpoint not in unfulfilled

        def test_unfulfilled_endpoints_includes_under_limit(self):
            # GIVEN a charm with limit 2
            limited_charm = Charm(
                name="limited-charm",
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
                            limit=2,
                        )
                    }
                ),
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
                            name="database",
                            interface="postgresql",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limit=None,
                        )
                    }
                ),
            )

            # AND a bundle with one integration (under limit)
            bundle = Bundle(
                applications=frozenset(
                    {Application(name="db", charm=limited_charm), Application(name="app", charm=requiring_charm)}
                ),
                integrations=frozenset(
                    {
                        Integration(
                            {
                                ApplicationEndpoint(application="db", endpoint="database"),
                                ApplicationEndpoint(application="app", endpoint="database"),
                            }
                        )
                    }
                ),
                platform="machine",
                arch="amd64",
            )

            # WHEN getting unfulfilled endpoints
            unfulfilled = bundle.unfulfilled_endpoints

            # THEN the limited endpoint should be fulfilled (already has one connection)
            db_endpoint = ApplicationEndpoint(application="db", endpoint="database")
            assert db_endpoint not in unfulfilled

    class TestEdgeCases:
        def test_zero_limit_blocks_all_connections(self):
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
                            limit=0,
                        )
                    }
                ),
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
                            limit=None,
                        )
                    }
                ),
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

            # WHEN checking if we can add integration
            builder = BundleBuilder(CharmhubClient())
            can_add = builder._can_add_integration_within_charm_limits(
                bundle,
                ApplicationEndpoint(application="server", endpoint="disabled"),
                ApplicationEndpoint(application="client", endpoint="http"),
            )

            # THEN it should be blocked
            assert can_add is False

        def test_limit_applies_to_both_endpoints_in_integration(self):
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
                            limit=1,
                        )
                    }
                ),
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
                            limit=1,
                        )
                    }
                ),
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

            # WHEN trying to add another integration
            builder = BundleBuilder(CharmhubClient())
            can_add = builder._can_add_integration_within_charm_limits(
                bundle,
                ApplicationEndpoint(application="app1", endpoint="api"),
                ApplicationEndpoint(application="app3", endpoint="upstream"),
            )

            # THEN it should be blocked (app1's endpoint reached limit)
            assert can_add is False

    class TestMultipleCharmInstances:
        """Test scenarios where multiple instances of the same charm are needed."""

        def test_multiple_postgresql_instances_for_dependencies(self):
            """Test the exact scenario from PR feedback where indico needs postgresql and a dependency that also needs postgresql."""
            from unittest.mock import MagicMock

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
                            limit=1,  # This is the key limitation
                        )
                    }
                ),
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
                            limit=1,
                        ),
                        CharmEndpoint(
                            type=ENDPOINT_REQUIRES,
                            name="juju-info",
                            interface="juju-info",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limit=1,
                        ),
                    }
                ),
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
                            limit=1,
                        ),
                        CharmEndpoint(
                            type=ENDPOINT_REQUIRES,
                            name="database",
                            interface="postgresql",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limit=1,
                        ),
                    }
                ),
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

            # AND a mock charmhub client
            mock_client = MagicMock(spec=CharmhubClient)

            def mock_find_charms(**kwargs):
                if kwargs.get("provides") == "juju-info":
                    return {"some-dependency-k8s"}
                elif kwargs.get("provides") == "postgresql":
                    return {"postgresql-k8s"}
                return set()

            def mock_charm_from_store(charm_name, **kwargs):
                if charm_name == "postgresql-k8s":
                    return postgresql_charm
                elif charm_name == "some-dependency-k8s":
                    return dependency_charm
                return None

            mock_client.find_charms.side_effect = mock_find_charms
            mock_client.charm_from_store.side_effect = mock_charm_from_store

            # WHEN building the bundle
            builder = BundleBuilder(mock_client)
            result = builder.build(base_bundle)

            # THEN it should have the original apps plus dependency and second postgresql
            app_names = {app.name for app in result.applications}

            assert "postgresql-k8s" in app_names
            assert "indico" in app_names
            assert "some-dependency-k8s" in app_names
            assert "postgresql-k8s-2" in app_names  # The second instance!

            # We should have at least 4 applications
            assert len(result.applications) >= 4

            # AND the integrations should be set up correctly
            integration_pairs = [{str(ep) for ep in integration} for integration in result.integrations]

            # Original postgresql connected to indico
            assert {"postgresql-k8s:database", "indico:database"} in integration_pairs

            # Dependency connected to indico via juju-info
            assert {"some-dependency-k8s:juju-info", "indico:juju-info"} in integration_pairs

            # Check that some-dependency-k8s has a database connection to SOME postgresql instance
            # (could be postgresql-k8s-2, postgresql-k8s-3, etc due to non-deterministic graph exploration)
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

        def test_prevents_infinite_charm_chain(self):
            """Test that self-referential charms don't create infinite chains."""
            from unittest.mock import MagicMock

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
                            limit=None,
                        ),
                        CharmEndpoint(
                            type=ENDPOINT_REQUIRES,
                            name="tracing-consumer",
                            interface="tracing",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limit=None,
                        ),
                    }
                ),
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
                            limit=None,
                        ),
                    }
                ),
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

            # AND a mock client that only returns grafana-agent for tracing
            mock_client = MagicMock(spec=CharmhubClient)
            mock_client.find_charms.return_value = {"grafana-agent-k8s"}
            mock_client.charm_from_store.return_value = self_ref_charm

            # WHEN building with a limit on same-charm instances
            builder = BundleBuilder(mock_client)
            builder.max_same_charm_instances = 3
            result = builder.build(base_bundle)

            # THEN it should not exceed the limit
            grafana_apps = [app for app in result.applications if app.charm.name == "grafana-agent-k8s"]
            assert len(grafana_apps) <= builder.max_same_charm_instances

            # AND it should stop adding new instances when limit is reached
            assert len(grafana_apps) < 10  # Definitely not infinite!
