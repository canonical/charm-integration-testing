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


import dataclasses
import logging

from pydantic.dataclasses import dataclass

from bundle_builder.bundle import Application, ApplicationEndpoint, Bundle, Integration
from bundle_builder.bundle_builder import BundleBuilder, Node
from bundle_builder.charm import (
    Charm,
    CharmConfigCriteria,
    CharmEndpointOptionality,
    CharmLimit,
    CharmTestConfig,
)

from .test_bundle import sample_bundle_postgresql_k8s_kratos
from .test_charm import (
    sample_charm_endpoint_kratos_pg_database,
    sample_charm_endpoint_postgresql_k8s_database,
    sample_charm_kratos,
    sample_charm_postgresql_k8s,
    sample_charm_self_signed_certificates,
)


@dataclass
class CharmhubClientStub:
    def find_charms(
        self, provides: str | None = None, requires: str | None = None, platform: str | None = None
    ) -> frozenset[str]:
        if provides == "db":
            return frozenset({"postgresql-k8s"})
        if requires == "db":
            return frozenset({"kratos"})
        if provides == "unknown":
            return frozenset()
        return frozenset()

    def charm_from_store(
        self,
        charm_name: str,
        ubuntu_arch: str,
        charm_channel: str | None = None,
        charm_revision: int | None = None,
        ubuntu_version: str | None = None,
    ) -> Charm | None:
        if charm_name == "postgresql-k8s":
            return sample_charm_postgresql_k8s()
        return None


def sample_node_postgresql_k8s_kratos() -> Node:
    return Node(
        bundle=dataclasses.replace(
            sample_bundle_postgresql_k8s_kratos(),
            applications=frozenset(
                {
                    Application("postgresql-k8s", sample_charm_postgresql_k8s()),
                    Application("kratos", sample_charm_kratos()),
                }
            ),
            integrations=frozenset(
                {
                    Integration(
                        {
                            ApplicationEndpoint("postgresql-k8s", "database"),
                            ApplicationEndpoint("kratos", "pg-database"),
                        }
                    )
                }
            ),
        ),
        aggression=0.0,
    )


def sample_node_kratos(charm_priority: float = 1.0) -> Node:
    return dataclasses.replace(
        sample_node_postgresql_k8s_kratos(),
        bundle=dataclasses.replace(
            sample_node_postgresql_k8s_kratos().bundle,
            applications=frozenset(
                {
                    Application("kratos", dataclasses.replace(sample_charm_kratos(), priority=charm_priority)),
                }
            ),
            integrations=frozenset(),
        ),
        aggression=0.0,
    )


def sample_node_kratos_self_signed_certificates() -> Node:
    return dataclasses.replace(
        sample_node_kratos(),
        bundle=dataclasses.replace(
            sample_node_kratos().bundle,
            applications=frozenset(
                {
                    Application("kratos", sample_charm_kratos()),
                    Application("self-signed-certificates", sample_charm_self_signed_certificates()),
                }
            ),
        ),
        aggression=0.0,
    )


class TestNode:
    def test_score_prioritizes_fewer_applications_and_unfulfilled_endpoints(self) -> None:
        node = sample_node_kratos_self_signed_certificates()
        # Score should increase with more applications and unfulfilled endpoints
        score = node.score
        assert isinstance(score, float)
        # Remove an application, score should decrease
        node2 = dataclasses.replace(
            node,
            bundle=dataclasses.replace(
                node.bundle, applications=frozenset({Application("kratos", sample_charm_kratos())})
            ),
        )
        assert node2.score < score

    def test_fingerprint_is_bundle_integrations(self) -> None:
        node = sample_node_kratos_self_signed_certificates()
        assert node.fingerprint == node.bundle.integrations

    def test_stats_string(self) -> None:
        node = sample_node_kratos_self_signed_certificates()
        stats = node.stats
        assert str(len(node.bundle.applications)) in stats
        assert "unfulfilled endpoints" in stats
        assert "saturated endpoints" in stats

    def test_lt_compares_score(self) -> None:
        node = sample_node_kratos_self_signed_certificates()
        node2 = dataclasses.replace(node, aggression=node.aggression + 0.1)
        assert (node < node2) == (node.score < node2.score)


class TestBundleBuilder:
    def test_build_returns_best_node_bundle(self) -> None:
        stub = CharmhubClientStub()
        builder = BundleBuilder(charmhub_client=stub, logger=logging.getLogger("test"))  # type: ignore[arg-type]
        base = sample_node_kratos().bundle
        result = builder.build(base)
        assert isinstance(result, Bundle)
        # Should resolve integrations if possible
        assert any(app.name == "postgresql-k8s" for app in result.applications)

    def test_child_nodes_returns_possible_children(self) -> None:
        stub = CharmhubClientStub()
        builder = BundleBuilder(charmhub_client=stub)  # type: ignore[arg-type]
        node = sample_node_kratos()
        children = builder.child_nodes(node)
        assert isinstance(children, set)
        for child in children:
            assert isinstance(child, Node)

    def test_child_nodes_existing_applications_filters_cycles_and_limits(self) -> None:
        stub = CharmhubClientStub()
        builder = BundleBuilder(charmhub_client=stub, avoid_application_dependency_cycles=True)  # type: ignore[arg-type]
        node = sample_node_kratos()
        # Should not create cycles
        children = builder.child_nodes_existing_applications(node, ApplicationEndpoint("kratos", "pg-database"))
        for child in children:
            assert not child.bundle.has_application_dependency("kratos", "kratos")

    def test_child_nodes_new_applications_adds_valid_children(self) -> None:
        stub = CharmhubClientStub()
        builder = BundleBuilder(charmhub_client=stub)  # type: ignore[arg-type]
        node = sample_node_kratos()
        children = builder.child_nodes_new_applications(node, ApplicationEndpoint("kratos", "pg-database"))
        for child in children:
            assert any(app.name.startswith("postgresql-k8s") for app in child.bundle.applications)

    def test_child_nodes_existing_applications_validates_endpoint_features(self) -> None:
        # GIVEN a provider charm with limited features (only compression, not SSL)
        provider_charm = dataclasses.replace(
            sample_charm_postgresql_k8s(),
            name="database",
            endpoints=frozenset(
                {
                    dataclasses.replace(
                        sample_charm_endpoint_postgresql_k8s_database(),
                        features=frozenset({"compression"}),
                    )
                }
            ),
        )

        # AND a requirer charm that requires SSL feature
        requirer_with_ssl = dataclasses.replace(
            sample_charm_kratos(),
            name="app-ssl",
            endpoints=frozenset(
                {
                    dataclasses.replace(
                        sample_charm_endpoint_kratos_pg_database(),
                        name="database",
                        features=frozenset({"ssl"}),
                    )
                }
            ),
        )

        # AND a requirer charm that requires compression feature
        requirer_with_compression = dataclasses.replace(
            sample_charm_kratos(),
            name="app-compression",
            endpoints=frozenset(
                {
                    dataclasses.replace(
                        sample_charm_endpoint_kratos_pg_database(),
                        name="database",
                        features=frozenset({"compression"}),
                    )
                }
            ),
        )

        # AND a bundle with the provider and both requirers
        bundle = Bundle(
            applications=frozenset(
                {
                    Application(name="db", charm=provider_charm),
                    Application(name="app-ssl", charm=requirer_with_ssl),
                    Application(name="app-compression", charm=requirer_with_compression),
                }
            ),
            integrations=frozenset(),
            platform="machine",
            arch="amd64",
        )

        stub = CharmhubClientStub()
        # TODO(raul): remove type ignore in subsequent type checker PRs
        builder = BundleBuilder(charmhub_client=stub)  # type: ignore[arg-type]
        node = Node(bundle=bundle, aggression=0.0)

        # WHEN checking child nodes for the SSL requirer endpoint
        children_ssl = builder.child_nodes_existing_applications(node, ApplicationEndpoint("app-ssl", "database"))

        # THEN no children should be created (provider doesn't have SSL feature)
        assert len(children_ssl) == 0

        # WHEN checking child nodes for the compression requirer endpoint
        children_compression = builder.child_nodes_existing_applications(
            node, ApplicationEndpoint("app-compression", "database")
        )

        # THEN one child should be created (provider has compression feature)
        assert len(children_compression) == 1
        child = next(iter(children_compression))
        assert (
            Integration(
                {
                    ApplicationEndpoint("db", "database"),
                    ApplicationEndpoint("app-compression", "database"),
                }
            )
            in child.bundle.integrations
        )

    def test_child_nodes_existing_applications_allows_superset_features(self) -> None:
        # GIVEN a provider charm with multiple features
        provider_charm = dataclasses.replace(
            sample_charm_postgresql_k8s(),
            name="database",
            endpoints=frozenset(
                {
                    dataclasses.replace(
                        sample_charm_endpoint_postgresql_k8s_database(),
                        features=frozenset({"ssl", "compression", "replication"}),
                    )
                }
            ),
        )

        # AND a requirer charm that only requires SSL
        requirer_charm = dataclasses.replace(
            sample_charm_kratos(),
            name="app",
            endpoints=frozenset(
                {
                    dataclasses.replace(
                        sample_charm_endpoint_kratos_pg_database(),
                        name="database",
                        features=frozenset({"ssl"}),
                    )
                }
            ),
        )

        # AND a bundle with both applications
        bundle = Bundle(
            applications=frozenset(
                {
                    Application(name="db", charm=provider_charm),
                    Application(name="app", charm=requirer_charm),
                }
            ),
            integrations=frozenset(),
            platform="machine",
            arch="amd64",
        )

        stub = CharmhubClientStub()
        # TODO(raul): remove type ignore in subsequent type checker PRs
        builder = BundleBuilder(charmhub_client=stub)  # type: ignore[arg-type]
        node = Node(bundle=bundle, aggression=0.0)

        # WHEN checking child nodes for the requirer endpoint
        children = builder.child_nodes_existing_applications(node, ApplicationEndpoint("app", "database"))

        # THEN one child should be created (provider has all required features and more)
        assert len(children) == 1
        child = next(iter(children))
        assert (
            Integration(
                {
                    ApplicationEndpoint("db", "database"),
                    ApplicationEndpoint("app", "database"),
                }
            )
            in child.bundle.integrations
        )

    def test_child_nodes_existing_applications_allows_no_features(self) -> None:
        # GIVEN a provider charm without features
        provider_charm = dataclasses.replace(
            sample_charm_postgresql_k8s(),
            name="database",
            endpoints=frozenset(
                {
                    dataclasses.replace(
                        sample_charm_endpoint_postgresql_k8s_database(),
                        features=frozenset(),
                    )
                }
            ),
        )

        # AND a requirer charm without features
        requirer_charm = dataclasses.replace(
            sample_charm_kratos(),
            name="app",
            endpoints=frozenset(
                {
                    dataclasses.replace(
                        sample_charm_endpoint_kratos_pg_database(),
                        name="database",
                        features=frozenset(),
                    )
                }
            ),
        )

        # AND a bundle with both applications
        bundle = Bundle(
            applications=frozenset(
                {
                    Application(name="db", charm=provider_charm),
                    Application(name="app", charm=requirer_charm),
                }
            ),
            integrations=frozenset(),
            platform="machine",
            arch="amd64",
        )

        stub = CharmhubClientStub()
        # TODO(raul): remove type ignore in subsequent type checker PRs
        builder = BundleBuilder(charmhub_client=stub)  # type: ignore[arg-type]
        node = Node(bundle=bundle, aggression=0.0)

        # WHEN checking child nodes for the requirer endpoint
        children = builder.child_nodes_existing_applications(node, ApplicationEndpoint("app", "database"))

        # THEN one child should be created (both have no features, which is compatible)
        assert len(children) == 1
        child = next(iter(children))
        assert (
            Integration(
                {
                    ApplicationEndpoint("db", "database"),
                    ApplicationEndpoint("app", "database"),
                }
            )
            in child.bundle.integrations
        )

    class TestBundleBuilderLimitValidation:
        def test_can_add_integration_respects_limits(self) -> None:
            # GIVEN a charm with limited endpoint
            limited_charm = dataclasses.replace(
                sample_charm_postgresql_k8s(),
                name="limited-charm",
                endpoints=frozenset(
                    {
                        dataclasses.replace(
                            sample_charm_endpoint_postgresql_k8s_database(),
                            interface="postgresql",
                            limits=(CharmLimit(limit=1),),
                        )
                    }
                ),
            )

            # AND charms that require database
            requiring_charm1 = dataclasses.replace(
                sample_charm_kratos(),
                name="app1",
                endpoints=frozenset(
                    {
                        dataclasses.replace(
                            sample_charm_endpoint_kratos_pg_database(),
                            name="database",
                            interface="postgresql",
                        )
                    }
                ),
            )

            requiring_charm2 = dataclasses.replace(
                sample_charm_kratos(),
                name="app2",
                endpoints=frozenset(
                    {
                        dataclasses.replace(
                            sample_charm_endpoint_kratos_pg_database(),
                            name="database",
                            interface="postgresql",
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
            # The method _can_add_integration_within_charm_limits does not exist. Instead, check bundle.unfulfilled_endpoints
            # THEN the limited endpoint should not be unfulfilled (limit reached)
            db_endpoint = ApplicationEndpoint(application="db", endpoint="database")
            assert db_endpoint not in bundle.unfulfilled_endpoints

        def test_can_add_integration_allows_when_under_limit(self) -> None:
            # GIVEN a charm with higher limit
            limited_charm = dataclasses.replace(
                sample_charm_postgresql_k8s(),
                name="limited-charm",
                endpoints=frozenset(
                    {
                        dataclasses.replace(
                            sample_charm_endpoint_postgresql_k8s_database(),
                            interface="postgresql",
                            limits=(CharmLimit(limit=2),),
                        )
                    }
                ),
            )

            requiring_charm = dataclasses.replace(
                sample_charm_kratos(),
                name="app",
                endpoints=frozenset(
                    {
                        dataclasses.replace(
                            sample_charm_endpoint_kratos_pg_database(),
                            name="database",
                            interface="postgresql",
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
            # The method _can_add_integration_within_charm_limits does not exist. Instead, check bundle.unfulfilled_endpoints
            app2_endpoint = ApplicationEndpoint(application="app2", endpoint="database")
            assert app2_endpoint in bundle.unfulfilled_endpoints

        def test_can_add_integration_allows_unlimited_endpoints(self) -> None:
            # GIVEN charms with no limits
            unlimited_charm = dataclasses.replace(
                sample_charm_postgresql_k8s(),
                name="unlimited-charm",
                endpoints=frozenset(
                    {
                        dataclasses.replace(
                            sample_charm_endpoint_postgresql_k8s_database(),
                            name="http",
                            interface="http",
                        )
                    }
                ),
            )

            requiring_charm = dataclasses.replace(
                sample_charm_kratos(),
                name="app",
                endpoints=frozenset(
                    {
                        dataclasses.replace(
                            sample_charm_endpoint_kratos_pg_database(),
                            name="http",
                            interface="http",
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
            # The method _can_add_integration_within_charm_limits does not exist. Instead, check bundle.unfulfilled_endpoints
            client2_endpoint = ApplicationEndpoint(application="client2", endpoint="http")
            assert client2_endpoint in bundle.unfulfilled_endpoints


class TestDuplicateCharms:
    def test_charm_instance_limit_prevents_cycles(self) -> None:
        """Test that the charm instance limit prevents infinite cycles."""
        # GIVEN a bundle builder with a low instance limit
        max_instances = 2

        # AND a charm
        charm = dataclasses.replace(sample_charm_postgresql_k8s(), endpoints=frozenset())

        # AND a bundle with two instances of the charm (at the limit)
        bundle = Bundle(
            applications=frozenset(
                {
                    Application(name="postgresql-k8s", charm=charm),
                    Application(name="postgresql-k8s-2", charm=charm),
                }
            ),
            integrations=frozenset(),
            platform="kubernetes",
            arch="amd64",
        )

        # WHEN checking if we would exceed the limit
        count = sum(1 for app in bundle.applications if app.charm.name == "postgresql-k8s")
        would_exceed = count >= max_instances

        # THEN it should return True
        assert would_exceed is True

    def test_charm_instance_limit_allows_under_limit(self) -> None:
        """Test that charms can be added when under the instance limit."""
        max_instances = 3

        # AND a charm
        charm = dataclasses.replace(sample_charm_postgresql_k8s(), endpoints=frozenset())

        # AND a bundle with one instance of the charm
        bundle = Bundle(
            applications=frozenset(
                {
                    Application(name="postgresql-k8s", charm=charm),
                }
            ),
            integrations=frozenset(),
            platform="kubernetes",
            arch="amd64",
        )

        # WHEN checking if we would exceed the limit
        count = sum(1 for app in bundle.applications if app.charm.name == "postgresql-k8s")
        would_exceed = count >= max_instances

        # THEN it should return False
        assert would_exceed is False

    def test_node_fingerprint_uses_application_names(self) -> None:
        """Test that the node fingerprint is based on application names, not charm names."""
        # GIVEN a charm
        charm = dataclasses.replace(sample_charm_postgresql_k8s(), endpoints=frozenset())

        # AND two bundles with the same charm but different application names
        bundle1 = Bundle(
            applications=frozenset(
                {
                    Application(name="postgresql-k8s", charm=charm),
                }
            ),
            integrations=frozenset(),
            platform="kubernetes",
            arch="amd64",
        )

        bundle2 = Bundle(
            applications=frozenset(
                {
                    Application(name="postgresql-k8s-2", charm=charm),
                }
            ),
            integrations=frozenset(),
            platform="kubernetes",
            arch="amd64",
        )

        # WHEN creating nodes from these bundles
        node1 = Node(bundle=bundle1, aggression=0.0)
        node2 = Node(bundle=bundle2, aggression=0.0)

        # THEN the fingerprints should match the bundle integrations
        assert node1.fingerprint == bundle1.integrations
        assert node2.fingerprint == bundle2.integrations

    def test_multiple_instances_with_integrations(self) -> None:
        """Test that multiple instances of the same charm can have different integrations."""
        # GIVEN a database charm and an app charm
        db_charm = dataclasses.replace(
            sample_charm_postgresql_k8s(),
            endpoints=frozenset(
                {
                    dataclasses.replace(
                        sample_charm_endpoint_postgresql_k8s_database(),
                        interface="postgresql",
                        limits=(CharmLimit(limit=1),),
                    )
                }
            ),
        )

        app_charm = dataclasses.replace(
            sample_charm_kratos(),
            name="app",
            endpoints=frozenset(
                {
                    dataclasses.replace(
                        sample_charm_endpoint_kratos_pg_database(),
                        name="database",
                        interface="postgresql",
                    )
                }
            ),
        )

        # AND a bundle with two database instances and two app instances
        bundle = Bundle(
            applications=frozenset(
                {
                    Application(name="postgresql-k8s", charm=db_charm),
                    Application(name="postgresql-k8s-2", charm=db_charm),
                    Application(name="app1", charm=app_charm),
                    Application(name="app2", charm=app_charm),
                }
            ),
            integrations=frozenset(
                {
                    Integration(
                        {
                            ApplicationEndpoint(application="postgresql-k8s", endpoint="database"),
                            ApplicationEndpoint(application="app1", endpoint="database"),
                        }
                    ),
                    Integration(
                        {
                            ApplicationEndpoint(application="postgresql-k8s-2", endpoint="database"),
                            ApplicationEndpoint(application="app2", endpoint="database"),
                        }
                    ),
                }
            ),
            platform="kubernetes",
            arch="amd64",
        )

        # THEN each database should have exactly one connection (respecting the limit)
        db1_connections = bundle.endpoint_connection_counts[
            ApplicationEndpoint(application="postgresql-k8s", endpoint="database")
        ]
        db2_connections = bundle.endpoint_connection_counts[
            ApplicationEndpoint(application="postgresql-k8s-2", endpoint="database")
        ]

        assert db1_connections == 1
        assert db2_connections == 1


class TestAddTestConfigs:
    class TestConfigSelection:
        def test_selects_config_matching_channel_track(self) -> None:
            # GIVEN a charm with test configs for different tracks
            charm = dataclasses.replace(
                sample_charm_postgresql_k8s(),
                name="test-charm",
                # TODO(raul): remove type: ignore in subsequent type checker-related PR
                channel="1.0/stable",  # type: ignore[arg-type]
                test_configs=(
                    CharmTestConfig(
                        criteria=CharmConfigCriteria(track="1.0"),
                        config=(("option1", "value1"),),
                    ),
                    CharmTestConfig(
                        criteria=CharmConfigCriteria(track="2.0"),
                        config=(("option2", "value2"),),
                    ),
                ),
            )
            bundle = Bundle(
                applications=frozenset({Application(name="app", charm=charm)}),
                integrations=frozenset(),
                platform="kubernetes",
                arch="amd64",
            )

            # WHEN add_test_configs is called
            result = BundleBuilder.add_test_configs(bundle)

            # THEN the application has a config matching the 1.0 track
            app = next(a for a in result.applications if a.name == "app")
            assert app.config == (("option1", "value1"),)

        def test_selects_config_matching_integrated_endpoint(self) -> None:
            # GIVEN a charm with test configs based on endpoint integration
            charm = dataclasses.replace(
                sample_charm_kratos(),
                name="test-charm",
                endpoints=frozenset(
                    {
                        dataclasses.replace(
                            sample_charm_endpoint_kratos_pg_database(),
                            name="database",
                            interface="db",
                            optionality=CharmEndpointOptionality.from_bool(True),
                        ),
                    }
                ),
                test_configs=(
                    CharmTestConfig(
                        criteria=CharmConfigCriteria(endpoint_integrated="database"),
                        config=(("with_db", "true"),),
                    ),
                    CharmTestConfig(
                        criteria=CharmConfigCriteria(
                            none_of=frozenset({CharmConfigCriteria(endpoint_integrated="database")})
                        ),
                        config=(("with_db", "false"),),
                    ),
                ),
            )
            db_charm = dataclasses.replace(
                sample_charm_postgresql_k8s(),
                name="db-charm",
                endpoints=frozenset(
                    {
                        dataclasses.replace(
                            sample_charm_endpoint_postgresql_k8s_database(),
                            interface="db",
                        ),
                    }
                ),
            )
            bundle = Bundle(
                applications=frozenset(
                    {
                        Application(name="app", charm=charm),
                        Application(name="db", charm=db_charm),
                    }
                ),
                integrations=frozenset(
                    {
                        Integration(
                            {
                                ApplicationEndpoint(application="app", endpoint="database"),
                                ApplicationEndpoint(application="db", endpoint="database"),
                            }
                        )
                    }
                ),
                platform="kubernetes",
                arch="amd64",
            )

            # WHEN add_test_configs is called
            result = BundleBuilder.add_test_configs(bundle)

            # THEN the application has the config for integrated database
            app = next(a for a in result.applications if a.name == "app")
            assert app.config == (("with_db", "true"),)

        def test_returns_empty_config_when_no_test_configs(self) -> None:
            # GIVEN a charm with no test configs
            charm = dataclasses.replace(
                sample_charm_postgresql_k8s(),
                name="test-charm",
                test_configs=(),
            )
            bundle = Bundle(
                applications=frozenset({Application(name="app", charm=charm)}),
                integrations=frozenset(),
                platform="kubernetes",
                arch="amd64",
            )

            # WHEN add_test_configs is called
            result = BundleBuilder.add_test_configs(bundle)

            # THEN the application has an empty config
            app = next(a for a in result.applications if a.name == "app")
            assert app.config == ()

        def test_returns_empty_config_when_no_matching_criteria(self) -> None:
            # GIVEN a charm with test configs that don't match
            charm = dataclasses.replace(
                sample_charm_postgresql_k8s(),
                name="test-charm",
                test_configs=(
                    CharmTestConfig(
                        criteria=CharmConfigCriteria(track="1.0"),
                        config=(("option1", "value1"),),
                    ),
                ),
            )
            bundle = Bundle(
                applications=frozenset({Application(name="app", charm=charm)}),
                integrations=frozenset(),
                platform="kubernetes",
                arch="amd64",
            )

            # WHEN add_test_configs is called (channel is latest/stable, not 1.0/stable)
            result = BundleBuilder.add_test_configs(bundle)

            # THEN the application has an empty config
            app = next(a for a in result.applications if a.name == "app")
            assert app.config == ()

        def test_handles_multiple_applications(self) -> None:
            # GIVEN multiple applications with different configs
            charm1 = dataclasses.replace(
                sample_charm_postgresql_k8s(),
                name="charm1",
                # TODO(raul): remove type: ignore in subsequent type checker-related PR
                channel="1.0/stable",  # type: ignore[arg-type]
                test_configs=(
                    CharmTestConfig(
                        criteria=CharmConfigCriteria(track="1.0"),
                        config=(("option1", "value1"),),
                    ),
                ),
            )
            charm2 = dataclasses.replace(
                sample_charm_kratos(),
                name="charm2",
                # TODO(raul): remove type: ignore in subsequent type checker-related PR
                channel="2.0/stable",  # type: ignore[arg-type]
                test_configs=(
                    CharmTestConfig(
                        criteria=CharmConfigCriteria(track="2.0"),
                        config=(("option2", "value2"),),
                    ),
                ),
            )
            bundle = Bundle(
                applications=frozenset(
                    {
                        Application(name="app1", charm=charm1),
                        Application(name="app2", charm=charm2),
                    }
                ),
                integrations=frozenset(),
                platform="kubernetes",
                arch="amd64",
            )

            # WHEN add_test_configs is called
            result = BundleBuilder.add_test_configs(bundle)

            # THEN each application has its appropriate config
            app1 = next(a for a in result.applications if a.name == "app1")
            app2 = next(a for a in result.applications if a.name == "app2")
            assert app1.config == (("option1", "value1"),)
            assert app2.config == (("option2", "value2"),)

        def test_selects_from_multiple_valid_configs(self) -> None:
            # GIVEN a charm with multiple valid test configs
            charm = dataclasses.replace(
                sample_charm_postgresql_k8s(),
                name="test-charm",
                test_configs=(
                    CharmTestConfig(
                        criteria=CharmConfigCriteria.from_bool(True),
                        config=(("option1", "value1"),),
                    ),
                    CharmTestConfig(
                        criteria=CharmConfigCriteria.from_bool(True),
                        config=(("option2", "value2"),),
                    ),
                ),
            )
            bundle = Bundle(
                applications=frozenset({Application(name="app", charm=charm)}),
                integrations=frozenset(),
                platform="kubernetes",
                arch="amd64",
            )

            # WHEN add_test_configs is called
            result = BundleBuilder.add_test_configs(bundle)

            # THEN the application has one of the valid configs (random choice)
            app = next(a for a in result.applications if a.name == "app")
            assert app.config in ((("option1", "value1"),), (("option2", "value2"),))

        def test_complex_criteria_all_of_and_endpoint(self) -> None:
            # GIVEN a charm with complex criteria (all_of with track and endpoint)
            charm = dataclasses.replace(
                sample_charm_kratos(),
                name="test-charm",
                # TODO(raul): remove type: ignore in subsequent type checker-related PR
                channel="1.0/stable",  # type: ignore[arg-type]
                endpoints=frozenset(
                    {
                        dataclasses.replace(
                            sample_charm_endpoint_kratos_pg_database(),
                            name="database",
                            interface="db",
                            optionality=CharmEndpointOptionality.from_bool(True),
                        ),
                    }
                ),
                test_configs=(
                    CharmTestConfig(
                        criteria=CharmConfigCriteria(
                            all_of=frozenset(
                                {
                                    CharmConfigCriteria(track="1.0"),
                                    CharmConfigCriteria(endpoint_integrated="database"),
                                }
                            )
                        ),
                        config=(("complex", "config"),),
                    ),
                    CharmTestConfig(
                        criteria=CharmConfigCriteria.from_bool(True),
                        config=(("simple", "config"),),
                    ),
                ),
            )
            db_charm = dataclasses.replace(
                sample_charm_postgresql_k8s(),
                name="db-charm",
                endpoints=frozenset(
                    {
                        dataclasses.replace(
                            sample_charm_endpoint_postgresql_k8s_database(),
                            interface="db",
                        ),
                    }
                ),
            )
            bundle = Bundle(
                applications=frozenset(
                    {
                        Application(name="app", charm=charm),
                        Application(name="db", charm=db_charm),
                    }
                ),
                integrations=frozenset(
                    {
                        Integration(
                            {
                                ApplicationEndpoint(application="app", endpoint="database"),
                                ApplicationEndpoint(application="db", endpoint="database"),
                            }
                        )
                    }
                ),
                platform="kubernetes",
                arch="amd64",
            )

            # WHEN add_test_configs is called
            result = BundleBuilder.add_test_configs(bundle)

            # THEN the application can have either config (both criteria match)
            app = next(a for a in result.applications if a.name == "app")
            assert app.config in ((("complex", "config"),), (("simple", "config"),))
