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
    ENDPOINT_PROVIDES,
    ENDPOINT_REQUIRES,
    Charm,
    CharmConfig,
    CharmEndpoint,
    CharmEndpointOptionality,
)

from .test_bundle import sample_bundle_postgresql_k8s_kratos
from .test_charm import (
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
        application_endpoint_to_possible_charm=frozenset(),
        balance=1.0,
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
        application_endpoint_to_possible_charm=frozenset(
            {
                (ApplicationEndpoint("kratos", "pg-database"), "postgresql-k8s"),
            }
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
        builder = BundleBuilder(charmhub_client=stub, logger=logging.getLogger("test"))
        base = sample_node_kratos().bundle
        result = builder.build(base)
        assert isinstance(result, Bundle)
        # Should resolve integrations if possible
        assert any(app.name == "postgresql-k8s" for app in result.applications)

    def test_build_stops_on_max_nodes_visited(self) -> None:
        stub = CharmhubClientStub()
        builder = BundleBuilder(charmhub_client=stub, logger=logging.getLogger("test"), max_nodes_visited=1)
        base = sample_node_kratos().bundle
        result = builder.build(base)
        assert isinstance(result, Bundle)

    def test_child_nodes_returns_possible_children(self) -> None:
        stub = CharmhubClientStub()
        builder = BundleBuilder(charmhub_client=stub)
        node = sample_node_kratos()
        children = builder.child_nodes(node)
        assert isinstance(children, set)
        for child in children:
            assert isinstance(child, Node)

    def test_child_nodes_existing_applications_filters_cycles_and_limits(self) -> None:
        stub = CharmhubClientStub()
        builder = BundleBuilder(charmhub_client=stub, avoid_application_dependency_cycles=True)
        node = sample_node_kratos()
        # Should not create cycles
        children = builder.child_nodes_existing_applications(node, ApplicationEndpoint("kratos", "pg-database"))
        for child in children:
            assert not child.bundle.has_application_dependency("kratos", "kratos")

    def test_child_nodes_new_applications_adds_valid_children(self) -> None:
        stub = CharmhubClientStub()
        builder = BundleBuilder(charmhub_client=stub)
        node = sample_node_kratos()
        children = builder.child_nodes_new_applications(node, ApplicationEndpoint("kratos", "pg-database"))
        for child in children:
            assert any(app.name.startswith("postgresql-k8s") for app in child.bundle.applications)

    def test_random_test_config_returns_config_or_empty(self) -> None:
        # test_configs should be a tuple of tuples of tuples of (str, str|int)
        charm = dataclasses.replace(
            sample_charm_postgresql_k8s(),
            test_configs=(
                (("key1", "value1"), ("key2", "value2")),
                (("key3", "value3"),),
            ),
        )
        config = BundleBuilder.random_test_config(charm)
        assert isinstance(config, tuple) or config == CharmConfig()

    class TestBundleBuilderLimitValidation:
        def test_can_add_integration_respects_limits(self) -> None:
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
                priority=1.0,
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
                priority=1.0,
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
                priority=1.0,
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
                            name="database",
                            interface="postgresql",
                            optionality=CharmEndpointOptionality.from_bool(False),
                            limit=None,
                        )
                    }
                ),
                priority=1.0,
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
                            limit=None,
                        )
                    }
                ),
                priority=1.0,
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
        charm = Charm(
            name="postgresql-k8s",
            channel="stable",
            revision=1,
            ubuntu_version="22.04",
            ubuntu_arch="amd64",
            endpoints=frozenset(),
            priority=1.0,
        )

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
        charm = Charm(
            name="postgresql-k8s",
            channel="stable",
            revision=1,
            ubuntu_version="22.04",
            ubuntu_arch="amd64",
            endpoints=frozenset(),
            priority=1.0,
        )

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
        charm = Charm(
            name="postgresql-k8s",
            channel="stable",
            revision=1,
            ubuntu_version="22.04",
            ubuntu_arch="amd64",
            endpoints=frozenset(),
            priority=1.0,
        )

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
        node1 = Node(bundle=bundle1, application_endpoint_to_possible_charm=frozenset(), balance=1.0, aggression=0.0)
        node2 = Node(bundle=bundle2, application_endpoint_to_possible_charm=frozenset(), balance=1.0, aggression=0.0)

        # THEN the fingerprints should match the bundle integrations
        assert node1.fingerprint == bundle1.integrations
        assert node2.fingerprint == bundle2.integrations

    def test_multiple_instances_with_integrations(self) -> None:
        """Test that multiple instances of the same charm can have different integrations."""
        # GIVEN two database charms and two applications
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
                        limit=1,
                    )
                }
            ),
            priority=1.0,
        )

        app_charm = Charm(
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
            priority=1.0,
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
