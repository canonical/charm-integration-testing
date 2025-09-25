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
from unittest.mock import MagicMock

import pytest
from pydantic import Field
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
from bundle_builder.charmhub import CharmhubClient

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

    def charm_from_store(
        self,
        charm_name: str,
        ubuntu_arch: str,
        charm_channel: str | None = None,
        charm_revision: int | None = None,
        ubuntu_version: str | None = None,
    ):
        if charm_name == "postgresql-k8s":
            return sample_charm_postgresql_k8s()


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
    )


def sample_node_kratos() -> Node:
    return dataclasses.replace(
        sample_node_postgresql_k8s_kratos(),
        bundle=dataclasses.replace(
            sample_node_postgresql_k8s_kratos().bundle,
            applications=frozenset(
                {
                    Application("kratos", sample_charm_kratos()),
                }
            ),
            integrations=frozenset(),
        ),
        application_endpoint_to_possible_charm=frozenset(
            {
                (ApplicationEndpoint("kratos", "pg-database"), "postgresql-k8s"),
            }
        ),
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
    )


class TestNode:
    class TestScore:
        @dataclass
        class Params:
            label: str
            node: Node
            score: float

        test_cases = [
            Params(
                label="prioritize_fulfillable_interfaces",
                node=dataclasses.replace(
                    sample_node_kratos_self_signed_certificates(),
                    balance=0.0,
                ),
                score=1.0,
            ),
            Params(
                label="prioritize_number_of_applications",
                node=dataclasses.replace(
                    sample_node_kratos_self_signed_certificates(),
                    balance=1.0,
                ),
                score=1.25,
            ),
            Params(
                label="prioritize_equally",
                node=dataclasses.replace(
                    sample_node_kratos_self_signed_certificates(),
                    balance=0.5,
                ),
                score=1.125,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params):
            # GIVEN the node
            node = params.node

            # WHEN score is called
            score = node.score

            # THEN matches expected
            assert score == params.score

    def test_fingerprint(self):
        # GIVEN the sample node with two charms
        node = sample_node_kratos_self_signed_certificates()

        # WHEN fingerprint property is accessed
        fingerprint = node.fingerprint

        # THEN fingerprint matches charms in bundle
        assert fingerprint == frozenset(
            {
                sample_charm_kratos().name,
                sample_charm_self_signed_certificates().name,
            }
        )

    def test_fulfillable_interfaces(self):
        # GIVEN the sample node missing kratos:pg-database
        node = sample_node_kratos_self_signed_certificates()

        # WHEN fulfillable interfaces property is accessed
        fulfillable_interfaces = node.fulfillable_interfaces

        # THEN fulfillable interfaces is database
        assert fulfillable_interfaces == frozenset({sample_charm_endpoint_kratos_pg_database().interface})

    def test_child_charms(self):
        # GIVEN the sample node with kratos-pg-database fulfillable by postgresql-k8s
        node = sample_node_kratos_self_signed_certificates()

        # WHEN child charms property is accessed
        child_charms = node.child_charms

        # THEN child charms is postgresql-k8s
        assert child_charms == frozenset({sample_charm_postgresql_k8s().name})

    def test_stats(self):
        # GIVEN the sample node
        node = sample_node_kratos_self_signed_certificates()

        # WHEN stats property is accessed
        stats = node.stats

        # THEN stats matches expected
        assert stats == "2 applications (1 unfulfilled, 1 fulfillable interfaces, and 0 saturated endpoints)"

    def test_lt(self):
        # GIVEN the sample node
        node = sample_node_kratos_self_signed_certificates()
        # AND a node with a lower score
        node_lower_score = dataclasses.replace(node, balance=0.0)
        assert node_lower_score.score < node.score

        # WHEN compared
        result = node_lower_score < node

        # THEN lower score node is less
        assert result


class TestBundleBuilder:
    class TestBuild:
        @dataclass
        class Params:
            label: str
            base_bundle: Bundle
            expected_bundle: Bundle
            charmhub_client: CharmhubClientStub = Field(default_factory=CharmhubClientStub)

        test_cases = [
            Params(
                label="base_is_minimal",
                base_bundle=sample_node_postgresql_k8s_kratos().bundle,
                expected_bundle=sample_node_postgresql_k8s_kratos().bundle,
            ),
            Params(
                label="fulfill_bundle",
                base_bundle=sample_node_kratos().bundle,
                expected_bundle=sample_node_postgresql_k8s_kratos().bundle,
            ),
            Params(
                label="unfulfillable_interface",
                base_bundle=dataclasses.replace(
                    sample_node_kratos().bundle,
                    applications=frozenset(
                        {
                            Application(
                                name="kratos",
                                charm=dataclasses.replace(
                                    sample_charm_kratos(),
                                    endpoints=frozenset(
                                        {
                                            dataclasses.replace(
                                                sample_charm_endpoint_kratos_pg_database(),
                                                interface="unknown",
                                            ),
                                        }
                                    ),
                                ),
                            ),
                        }
                    ),
                ),
                expected_bundle=dataclasses.replace(
                    sample_node_kratos().bundle,
                    applications=frozenset(
                        {
                            Application(
                                name="kratos",
                                charm=dataclasses.replace(
                                    sample_charm_kratos(),
                                    endpoints=frozenset(
                                        {
                                            dataclasses.replace(
                                                sample_charm_endpoint_kratos_pg_database(),
                                                interface="unknown",
                                            ),
                                        }
                                    ),
                                ),
                            ),
                        }
                    ),
                ),
            ),
            Params(
                label="unfulfillable_interface",
                base_bundle=dataclasses.replace(
                    sample_node_kratos().bundle,
                    applications=frozenset(
                        {
                            Application(
                                name="kratos",
                                charm=dataclasses.replace(
                                    sample_charm_kratos(),
                                    endpoints=frozenset(
                                        {
                                            dataclasses.replace(
                                                sample_charm_endpoint_kratos_pg_database(),
                                                interface="unknown",
                                            ),
                                        }
                                    ),
                                ),
                            ),
                        }
                    ),
                ),
                expected_bundle=dataclasses.replace(
                    sample_node_kratos().bundle,
                    applications=frozenset(
                        {
                            Application(
                                name="kratos",
                                charm=dataclasses.replace(
                                    sample_charm_kratos(),
                                    endpoints=frozenset(
                                        {
                                            dataclasses.replace(
                                                sample_charm_endpoint_kratos_pg_database(),
                                                interface="unknown",
                                            ),
                                        }
                                    ),
                                ),
                            ),
                        }
                    ),
                ),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params):
            # GIVEN the base bundle
            base_bundle = params.base_bundle

            # WHEN the minimal bundle is build
            minimal_bundle = BundleBuilder(
                charmhub_client=params.charmhub_client
            ).build(base_bundle)

            # THEN matches expected bundle
            assert minimal_bundle == params.expected_bundle

    class TestAddMissingIntegrations:
        @dataclass
        class Params:
            label: str
            bundle: Bundle
            possible_integrations: list[frozenset[Integration]]
            charmhub_client: CharmhubClientStub = Field(default_factory=CharmhubClientStub)

        test_cases = [
            Params(
                label="no_missing_integrations",
                bundle=sample_bundle_postgresql_k8s_kratos(),
                possible_integrations=[sample_bundle_postgresql_k8s_kratos().integrations],
            ),
            Params(
                label="missing_integration",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    integrations=frozenset(),
                ),
                possible_integrations=[sample_bundle_postgresql_k8s_kratos().integrations],
            ),
            Params(
                label="do_not_integrate_self",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    applications=frozenset(
                        {
                            Application(
                                name="postgresql-k8s",
                                charm=dataclasses.replace(
                                    sample_charm_postgresql_k8s(),
                                    endpoints=frozenset(
                                        {
                                            sample_charm_endpoint_postgresql_k8s_database(),
                                            dataclasses.replace(
                                                sample_charm_endpoint_postgresql_k8s_database(),
                                                type=ENDPOINT_REQUIRES,
                                                name="required-database",
                                                optionality=CharmEndpointOptionality.from_bool(False),
                                            ),
                                        }
                                    ),
                                ),
                            )
                        }
                    ),
                    integrations=frozenset(),
                ),
                possible_integrations=[frozenset()],
            ),
            Params(
                label="do_not_integrate_different_interface",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    applications=frozenset(
                        {
                            Application("kratos", sample_charm_kratos()),
                            Application(
                                name="postgresql-k8s",
                                charm=dataclasses.replace(
                                    sample_charm_postgresql_k8s(),
                                    endpoints=frozenset(
                                        {
                                            dataclasses.replace(
                                                sample_charm_endpoint_postgresql_k8s_database(),
                                                interface="not-db",
                                            ),
                                        }
                                    ),
                                ),
                            ),
                        }
                    ),
                    integrations=frozenset(),
                ),
                possible_integrations=[frozenset()],
            ),
            Params(
                label="do_not_integrate_requires_together",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    applications=frozenset(
                        {
                            Application("kratos", sample_charm_kratos()),
                            Application(
                                name="postgresql-k8s",
                                charm=dataclasses.replace(
                                    sample_charm_postgresql_k8s(),
                                    endpoints=frozenset(
                                        {
                                            dataclasses.replace(
                                                sample_charm_endpoint_postgresql_k8s_database(),
                                                type=ENDPOINT_REQUIRES,
                                            ),
                                        }
                                    ),
                                ),
                            ),
                        }
                    ),
                    integrations=frozenset(),
                ),
                possible_integrations=[frozenset()],
            ),
            Params(
                label="do_not_integrate_provides_together",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    applications=frozenset(
                        {
                            Application("postgresql-k8s", sample_charm_postgresql_k8s()),
                            Application(
                                name="kratos",
                                charm=dataclasses.replace(
                                    sample_charm_kratos(),
                                    endpoints=frozenset(
                                        {
                                            dataclasses.replace(
                                                sample_charm_endpoint_kratos_pg_database(),
                                                type=ENDPOINT_PROVIDES,
                                            ),
                                        }
                                    ),
                                ),
                            ),
                        }
                    ),
                    integrations=frozenset(),
                ),
                possible_integrations=[frozenset()],
            ),
            Params(
                label="mutually_exclusive_endpoints",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    applications=frozenset(
                        {
                            Application("postgresql-k8s", sample_charm_postgresql_k8s()),
                            Application(
                                name="kratos",
                                charm=dataclasses.replace(
                                    sample_charm_kratos(),
                                    endpoints=frozenset(
                                        {
                                            dataclasses.replace(
                                                sample_charm_endpoint_kratos_pg_database(),
                                                name="pg-database-1",
                                                optionality=CharmEndpointOptionality(
                                                    endpoint_integrated="pg-database-2"
                                                ),
                                            ),
                                            dataclasses.replace(
                                                sample_charm_endpoint_kratos_pg_database(),
                                                name="pg-database-2",
                                                optionality=CharmEndpointOptionality(
                                                    endpoint_integrated="pg-database-1"
                                                ),
                                            ),
                                        },
                                    ),
                                ),
                            ),
                        }
                    ),
                    integrations=frozenset(),
                ),
                possible_integrations=[
                    frozenset(
                        {
                            Integration(
                                {
                                    ApplicationEndpoint("postgresql-k8s", "database"),
                                    ApplicationEndpoint("kratos", "pg-database-1"),
                                }
                            )
                        }
                    ),
                    frozenset(
                        {
                            Integration(
                                {
                                    ApplicationEndpoint("postgresql-k8s", "database"),
                                    ApplicationEndpoint("kratos", "pg-database-2"),
                                }
                            )
                        }
                    ),
                ],
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params):
            # GIVEN the bundle
            bundle = params.bundle

            # WHEN missing integrations are added
            new_bundle = BundleBuilder(charmhub_client=params.charmhub_client).add_missing_integrations(bundle)

            # THEN integrations match expected
            assert new_bundle.integrations in params.possible_integrations

    class TestNewNode:
        @dataclass
        class Params:
            label: str
            bundle: Bundle
            expected_integrations: frozenset[Integration]
            expected_application_endpoint_to_possible_charm: frozenset[tuple[ApplicationEndpoint, str]]
            expected_balance: float = 1.0
            charmhub_client: CharmhubClientStub = Field(default_factory=CharmhubClientStub)
            balance: float | None = None

        test_cases = [
            Params(
                label="fulfilled_bundle",
                bundle=sample_bundle_postgresql_k8s_kratos(),
                expected_integrations=sample_bundle_postgresql_k8s_kratos().integrations,
                expected_application_endpoint_to_possible_charm=frozenset(),
            ),
            Params(
                label="set_balance",
                bundle=sample_bundle_postgresql_k8s_kratos(),
                expected_integrations=sample_bundle_postgresql_k8s_kratos().integrations,
                expected_application_endpoint_to_possible_charm=frozenset(),
                expected_balance=0.5,
                balance=0.5,
            ),
            Params(
                label="fulfillable_bundle",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    integrations=frozenset(),
                ),
                expected_integrations=sample_bundle_postgresql_k8s_kratos().integrations,
                expected_application_endpoint_to_possible_charm=frozenset(),
            ),
            Params(
                label="unfulfilled_bundle",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    applications=frozenset(
                        {
                            Application(
                                name="kratos",
                                charm=sample_charm_kratos(),
                            ),
                        }
                    ),
                    integrations=frozenset(),
                ),
                expected_integrations=frozenset(),
                expected_application_endpoint_to_possible_charm=frozenset(
                    {(ApplicationEndpoint("kratos", "pg-database"), "postgresql-k8s")}
                ),
            ),
            Params(
                label="provides_endpoint_non_optional",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    applications=frozenset(
                        {
                            Application(
                                name="postgresql-k8s",
                                charm=dataclasses.replace(
                                    sample_charm_postgresql_k8s(),
                                    endpoints=frozenset(
                                        {
                                            dataclasses.replace(
                                                sample_charm_endpoint_postgresql_k8s_database(),
                                                type=ENDPOINT_PROVIDES,
                                                optionality=CharmEndpointOptionality.from_bool(False),
                                            )
                                        }
                                    ),
                                ),
                            ),
                        }
                    ),
                    integrations=frozenset(),
                ),
                expected_integrations=frozenset(),
                expected_application_endpoint_to_possible_charm=frozenset(
                    {(ApplicationEndpoint("postgresql-k8s", "database"), "kratos")}
                ),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params):
            # GIVEN the bundle
            bundle = params.bundle

            # WHEN a node is created for it
            node = BundleBuilder(charmhub_client=params.charmhub_client).new_node(
                bundle, **({"balance": params.balance} if params.balance else {})
            )

            # THEN expected integrations match in the bundle
            assert node.bundle.integrations == params.expected_integrations
            # AND the potential fulfillments match
            assert node.application_endpoint_to_possible_charm == params.expected_application_endpoint_to_possible_charm
            # AND the balance matches
            assert node.balance == params.expected_balance

    class TestChildNodes:
        @dataclass
        class Params:
            label: str
            node: Node
            children: frozenset[Node]
            charmhub_client: CharmhubClientStub = Field(default_factory=CharmhubClientStub)

        test_cases = [
            Params(
                label="no_children",
                node=sample_node_postgresql_k8s_kratos(),
                children=frozenset(),
            ),
            Params(
                label="has_child",
                node=sample_node_kratos(),
                children=frozenset({sample_node_postgresql_k8s_kratos()}),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params):
            # GIVEN the node
            node = params.node

            # WHEN the children nodes are requested
            children = BundleBuilder(charmhub_client=params.charmhub_client).child_nodes(node)

            # THEN matches expected children
            assert children == params.children

    class TestRandomTestConfig:
        @dataclass
        class Params:
            label: str
            charm: Charm
            expected: CharmConfig

        test_cases = [
            Params(
                label="no_test_configs",
                charm=dataclasses.replace(
                    sample_charm_postgresql_k8s(),
                    test_configs=(),
                ),
                expected=CharmConfig(),
            ),
            Params(
                label="one_test_config",
                charm=dataclasses.replace(
                    sample_charm_postgresql_k8s(),
                    test_configs=((("key", "value-1"),),),
                ),
                expected=(("key", "value-1"),),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params):
            # GIVEN the charm
            charm = params.charm

            # WHEN a random config is requested
            config = BundleBuilder.random_test_config(charm)

            # THEN matches expected config
            assert config == params.expected

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
                priority=1.0
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


class TestDuplicateCharms:
    """Test that the bundle builder can handle multiple instances of the same charm."""

    def test_unique_application_name_generation(self):
        """Test that unique application names are generated for duplicate charms."""
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

        # AND a bundle with one instance of the charm
        bundle = Bundle(
            applications=frozenset({Application(name="postgresql-k8s", charm=charm)}),
            integrations=frozenset(),
            platform="kubernetes",
            arch="amd64",
        )

        # WHEN generating unique names for the same charm
        name1 = bundle.generate_unique_application_name("postgresql-k8s")

        # THEN it should generate a suffixed name
        assert name1 == "postgresql-k8s-2"

        # AND when we add that application and generate another name
        bundle2 = Bundle(
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

        name2 = bundle2.generate_unique_application_name("postgresql-k8s")

        # THEN it should generate the next suffix
        assert name2 == "postgresql-k8s-3"

    def test_unique_application_name_for_new_charm(self):
        """Test that the base name is used when the charm is not in the bundle."""
        # GIVEN an empty bundle
        bundle = Bundle(
            applications=frozenset(),
            integrations=frozenset(),
            platform="kubernetes",
            arch="amd64",
        )

        # WHEN generating a name for a new charm
        name = bundle.generate_unique_application_name("postgresql-k8s")

        # THEN it should use the base name
        assert name == "postgresql-k8s"

    def test_get_application_names_for_charm(self):
        """Test getting all application names that use a specific charm."""
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

        # AND a bundle with multiple instances of the charm
        bundle = Bundle(
            applications=frozenset(
                {
                    Application(name="postgresql-k8s", charm=charm),
                    Application(name="postgresql-k8s-2", charm=charm),
                    Application(name="postgresql-k8s-3", charm=charm),
                }
            ),
            integrations=frozenset(),
            platform="kubernetes",
            arch="amd64",
        )

        # WHEN getting application names for the charm
        names = bundle.get_application_names_for_charm("postgresql-k8s")

        # THEN it should return all three application names
        assert names == frozenset({"postgresql-k8s", "postgresql-k8s-2", "postgresql-k8s-3"})

    def test_charm_instance_limit_prevents_cycles(self):
        """Test that the charm instance limit prevents infinite cycles."""
        # GIVEN a bundle builder with a low instance limit
        builder = BundleBuilder(MagicMock())
        builder.max_same_charm_instances = 2

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
        would_exceed = builder._would_exceed_charm_instance_limit(bundle, "postgresql-k8s")

        # THEN it should return True
        assert would_exceed is True

    def test_charm_instance_limit_allows_under_limit(self):
        """Test that charms can be added when under the instance limit."""
        # GIVEN a bundle builder
        builder = BundleBuilder(MagicMock())
        builder.max_same_charm_instances = 3

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
        would_exceed = builder._would_exceed_charm_instance_limit(bundle, "postgresql-k8s")

        # THEN it should return False
        assert would_exceed is False

    def test_node_fingerprint_uses_application_names(self):
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
        builder = BundleBuilder(MagicMock())
        node1 = builder.new_node(bundle1)
        node2 = builder.new_node(bundle2)

        # THEN the fingerprints should be different
        assert node1.fingerprint != node2.fingerprint
        assert node1.fingerprint == frozenset({"postgresql-k8s"})
        assert node2.fingerprint == frozenset({"postgresql-k8s-2"})

    def test_multiple_instances_with_integrations(self):
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
