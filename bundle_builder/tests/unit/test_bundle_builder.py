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
from dataclasses import field

import pytest
from pydantic.dataclasses import dataclass

from bundle_builder.bundle import Application, ApplicationEndpoint, Bundle, Integration
from bundle_builder.bundle_builder import BundleBuilder, Node
from bundle_builder.charm import ENDPOINT_PROVIDES, ENDPOINT_REQUIRES, CharmEndpointOptionality

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
                score=2.0,
            ),
            Params(
                label="prioritize_equally",
                node=dataclasses.replace(
                    sample_node_kratos_self_signed_certificates(),
                    balance=0.5,
                ),
                score=1.5,
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
        assert stats == "2 applications (1 unfulfilled and 1 fulfillable interfaces)"

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
            charmhub_client: CharmhubClientStub = field(default_factory=CharmhubClientStub)

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
            minimal_bundle = BundleBuilder(charmhub_client=params.charmhub_client).build(base_bundle)

            # THEN matches expected bundle
            assert minimal_bundle == params.expected_bundle

    class TestAddMissingIntegrations:
        @dataclass
        class Params:
            label: str
            bundle: Bundle
            possible_integrations: list[frozenset[Integration]]
            charmhub_client: CharmhubClientStub = field(default_factory=CharmhubClientStub)

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
            charmhub_client: CharmhubClientStub = field(default_factory=CharmhubClientStub)
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
            charmhub_client: CharmhubClientStub = field(default_factory=CharmhubClientStub)

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
