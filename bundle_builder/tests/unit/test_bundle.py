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
from linecache import cache
from typing import Optional

import pytest
import yaml
from pydantic.dataclasses import dataclass

from bundle_builder.bundle import Application, ApplicationEndpoint, Bundle, Integration
from bundle_builder.charm import ENDPOINT_PROVIDES, ENDPOINT_REQUIRES, Charm, CharmEndpoint, CharmEndpointOptionality
from bundle_builder.charmhub import CharmhubClient
from bundle_builder.charmhub_http import CharmhubBase
from bundle_builder.overrides import CharmEndpointOverride, CharmMetadataOverride, OverridesClient

from .test_charm import (
    sample_charm_endpoint_kratos_pg_database,
    sample_charm_endpoint_pgbouncer_k8s_backend_database,
    sample_charm_endpoint_pgbouncer_k8s_database,
    sample_charm_endpoint_postgresql_k8s_certificates,
    sample_charm_endpoint_postgresql_k8s_database,
    sample_charm_endpoint_self_signed_certificates_certificates,
    sample_charm_kratos,
    sample_charm_pgbouncer_k8s,
    sample_charm_postgresql_k8s,
    sample_charm_self_signed_certificates,
)


class TestApplication:
    class TestRepr:
        @dataclass
        class Params:
            label: str
            application: Application
            repr: str

        test_cases = [
            Params(
                label="application_has_charm_name",
                application=Application(name=sample_charm_postgresql_k8s().name, charm=sample_charm_postgresql_k8s()),
                repr=sample_charm_postgresql_k8s().name,
            ),
            Params(
                label="application_has_unique_name",
                application=Application(name="my-application", charm=sample_charm_postgresql_k8s()),
                repr=f"my-application({sample_charm_postgresql_k8s()})",
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN the application
            application = params.application

            # WHEN repr is called
            repr = application.__repr__()

            # THEN matches expected
            assert repr == params.repr


class TestLimitParsing:
    def test_charm_endpoint_override_limit(self) -> None:
        # GIVEN an override with limit
        override = CharmEndpointOverride(limit=3)

        # THEN limit is correctly set
        assert override.limit == 3

    def test_charm_endpoint_override_no_limit(self) -> None:
        # GIVEN an override without limit
        override = CharmEndpointOverride()

        # THEN limit is None
        assert override.limit is None


class TestLimitApplication:
    def test_charmhub_client_applies_limit_overrides(self) -> None:
        # Mock overrides client that returns limit overrides
        class MockOverridesClient(OverridesClient):
            @cache
            def get_charm_metadata_overrides(self, charm: str) -> CharmMetadataOverride:
                if charm == "test-charm":
                    return CharmMetadataOverride(provides={"database": CharmEndpointOverride(limit=2)})
                return CharmMetadataOverride()

        # Mock HTTP client that returns mock charm data
        class MockHttpClient:
            def refresh(self, action: object) -> "MockResponse":
                class MockResponse:
                    def __init__(self) -> None:
                        class MockError:
                            def __init__(self) -> None:
                                self.code = "invalid-charm-base"

                                class MockExtra:
                                    def __init__(self) -> None:
                                        self.default_bases = [MockBase()]

                                self.extra = MockExtra()
                        # Mock error for default base lookup (when base is "NA")
                        self.error: Optional[MockError] = None
                        if hasattr(action, "base") and action.base.name == "NA":
                            self.error = MockError()

                        self.name = "test-charm"
                        self.effective_channel = "stable"

                        class MockCharm:
                            def __init__(self) -> None:
                                self.revision = 1
                                self.bases = [MockBase()]

                                class MockMetadata:
                                    def __init__(self) -> None:
                                        class MockEndpoint:
                                            def __init__(self, interface, optional=None) -> None:
                                                self.interface = interface
                                                self.optional = optional

                                        self.provides = {"database": MockEndpoint("postgresql")}
                                        self.requires: dict[str, MockEndpoint] = {}
                                        self.peers: dict[str, MockEndpoint] = {}

                                self.metadata = MockMetadata()

                        self.charm = MockCharm()

                return MockResponse()

        def MockBase() -> CharmhubBase:
            return CharmhubBase(name="ubuntu", architecture="amd64", channel="22.04")

        # Create CharmhubClient with mock dependencies
        client = CharmhubClient(http_client=MockHttpClient(), overrides_client=MockOverridesClient())

        # WHEN getting charm from store
        charm = client.charm_from_store("test-charm", "amd64")
        assert charm is not None

        # THEN the endpoint has the correct limit
        database_endpoint = next(e for e in charm.endpoints if e.name == "database")
        assert database_endpoint.limit == 2


class TestApplicationEndpoint:
    sample_application_endpoint = ApplicationEndpoint("postgresql-k8s", "certificates")

    def test_repr(self) -> None:
        # GIVEN an application endpoint
        application_endpoint = self.sample_application_endpoint

        # WHEN repr is called
        repr = application_endpoint.__repr__()

        # THEN repr is application:endpoint
        assert repr == f"{application_endpoint.application}:{application_endpoint.endpoint}"

    def test_str(self) -> None:
        # GIVEN an application endpoint
        application_endpoint = self.sample_application_endpoint

        # WHEN str is called
        str = application_endpoint.__str__()

        # THEN str matches repr
        assert str == application_endpoint.__repr__()


def sample_bundle_postgresql_k8s_kratos() -> Bundle:
    return Bundle(
        applications=frozenset(
            {
                Application(
                    name="target",
                    charm=sample_charm_postgresql_k8s(),
                    config=(("config-option", "config-value"),),
                ),
                Application(
                    name="neighbor",
                    charm=sample_charm_kratos(),
                ),
            }
        ),
        integrations=frozenset(
            {
                Integration(
                    {
                        ApplicationEndpoint("target", sample_charm_endpoint_postgresql_k8s_database().name),
                        ApplicationEndpoint("neighbor", sample_charm_endpoint_kratos_pg_database().name),
                    }
                )
            }
        ),
        platform="kubernetes",
        arch="amd64",
    )


class TestBundle:
    class TestValidate:
        @dataclass
        class Params:
            label: str
            bundle: Bundle
            should_raise: bool
            match: str | None = None

        test_cases = [
            Params(
                label="valid_bundle",
                bundle=sample_bundle_postgresql_k8s_kratos(),
                should_raise=False,
            ),
            Params(
                label="duplicate_application_names",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    applications=frozenset(
                        {
                            Application(name="dup", charm=sample_charm_kratos()),
                            Application(name="dup", charm=sample_charm_postgresql_k8s()),
                        }
                    ),
                ),
                should_raise=True,
                match="Application names must be unique",
            ),
            Params(
                label="more_than_two_endpoints_in_integration",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    integrations=frozenset(
                        {
                            Integration(
                                {
                                    ApplicationEndpoint("target", sample_charm_endpoint_postgresql_k8s_database().name),
                                    ApplicationEndpoint("neighbor", sample_charm_endpoint_kratos_pg_database().name),
                                    ApplicationEndpoint(
                                        "target", sample_charm_endpoint_postgresql_k8s_certificates().name
                                    ),
                                }
                            )
                        }
                    ),
                ),
                should_raise=True,
                match="connect exactly two endpoints",
            ),
            Params(
                label="unknown_application_in_integration",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    integrations=frozenset(
                        {
                            Integration(
                                {
                                    ApplicationEndpoint("target", sample_charm_endpoint_postgresql_k8s_database().name),
                                    ApplicationEndpoint("unknown", sample_charm_endpoint_kratos_pg_database().name),
                                }
                            )
                        }
                    ),
                ),
                should_raise=True,
                match="Integration references unknown endpoint",
            ),
            Params(
                label="unknown_endpoint_in_integration",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    integrations=frozenset(
                        {
                            Integration(
                                {
                                    ApplicationEndpoint("target", sample_charm_endpoint_postgresql_k8s_database().name),
                                    ApplicationEndpoint("neighbor", "unknown"),
                                }
                            )
                        }
                    ),
                ),
                should_raise=True,
                match="Integration references unknown endpoint",
            ),
            Params(
                label="different_interface_types_in_integration",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    applications=frozenset(
                        {
                            Application(
                                name="app1",
                                charm=sample_charm_kratos(),
                            ),
                            Application(
                                name="app2",
                                charm=sample_charm_kratos(),
                            ),
                        }
                    ),
                    integrations=frozenset(
                        {
                            Integration(
                                {
                                    ApplicationEndpoint("app1", sample_charm_endpoint_kratos_pg_database().name),
                                    ApplicationEndpoint("app2", sample_charm_endpoint_kratos_pg_database().name),
                                }
                            )
                        }
                    ),
                ),
                should_raise=True,
                match="Incompatible endpoint types in integration",
            ),
            Params(
                label="endpoint_limit_exceeded",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    applications=frozenset(
                        {
                            Application(
                                name="db",
                                charm=dataclasses.replace(
                                    sample_charm_postgresql_k8s(),
                                    endpoints=frozenset(
                                        {dataclasses.replace(sample_charm_endpoint_postgresql_k8s_database(), limit=1)}
                                    ),
                                ),
                            ),
                            Application(
                                name="app1",
                                charm=sample_charm_kratos(),
                            ),
                            Application(
                                name="app2",
                                charm=sample_charm_kratos(),
                            ),
                        }
                    ),
                    integrations=frozenset(
                        {
                            Integration(
                                {
                                    ApplicationEndpoint("db", sample_charm_endpoint_postgresql_k8s_database().name),
                                    ApplicationEndpoint("app1", sample_charm_endpoint_kratos_pg_database().name),
                                }
                            ),
                            Integration(
                                {
                                    ApplicationEndpoint("db", sample_charm_endpoint_postgresql_k8s_database().name),
                                    ApplicationEndpoint("app2", sample_charm_endpoint_kratos_pg_database().name),
                                }
                            ),
                        }
                    ),
                ),
                should_raise=True,
                match="exceeding its limit",
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test_validate(self, params: Params) -> None:
            if params.should_raise:
                with pytest.raises(ValueError, match=params.match if params.match else ""):
                    params.bundle.validate()
            else:
                # should not raise
                params.bundle.validate()

    def test_application_endpoints(self) -> None:
        # GIVEN a bundle
        bundle = sample_bundle_postgresql_k8s_kratos()

        # WHEN the application endpoints property is fetched
        application_endpoints = bundle.application_endpoints

        # THEN matches
        assert application_endpoints == {
            ApplicationEndpoint("target", "certificates"): sample_charm_endpoint_postgresql_k8s_certificates(),
            ApplicationEndpoint("target", "database"): sample_charm_endpoint_postgresql_k8s_database(),
            ApplicationEndpoint(
                "neighbor", sample_charm_endpoint_kratos_pg_database().name
            ): sample_charm_endpoint_kratos_pg_database(),
        }

    class TestCharms:
        @dataclass
        class Params:
            label: str
            bundle: Bundle
            charms: set[str]

        test_cases = [
            Params(
                label="no_charms",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    applications=frozenset(),
                ),
                charms=frozenset(),
            ),
            Params(
                label="multiple_charms",
                bundle=sample_bundle_postgresql_k8s_kratos(),
                charms=frozenset({sample_charm_postgresql_k8s().name, sample_charm_kratos().name}),
            ),
            Params(
                label="duplicate_charms",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    applications=frozenset(
                        {
                            Application(name="kratos-1", charm=sample_charm_kratos()),
                            Application(name="kratos-2", charm=sample_charm_kratos()),
                        }
                    ),
                ),
                charms=frozenset({sample_charm_kratos().name}),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN the bundle
            bundle = params.bundle

            # WHEN charms property is called
            charms = bundle.charms

            # THEN charms match
            assert charms == params.charms

    class TestUnfulfilledEndpoints:
        @dataclass
        class Params:
            label: str
            bundle: Bundle
            unfulfilled_endpoints: set[ApplicationEndpoint]

        test_cases = [
            Params(
                label="no_non_optional_endpoints",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    applications=frozenset(
                        {
                            Application(name="target", charm=sample_charm_postgresql_k8s()),
                        }
                    ),
                    integrations=frozenset(),
                ),
                unfulfilled_endpoints=frozenset(),
            ),
            Params(
                label="missing_non_optional_endpoint",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    applications=frozenset(
                        {
                            Application(name="neighbor", charm=sample_charm_kratos()),
                        }
                    ),
                    integrations=frozenset(),
                ),
                unfulfilled_endpoints=frozenset(
                    {ApplicationEndpoint("neighbor", sample_charm_endpoint_kratos_pg_database().name)}
                ),
            ),
            Params(
                label="non_optional_endpoint_fulfilled",
                bundle=sample_bundle_postgresql_k8s_kratos(),
                unfulfilled_endpoints=frozenset(),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN the bundle
            bundle = params.bundle

            # WHEN unfulfilled endpoints is called
            unfulfilled_endpoints = bundle.unfulfilled_endpoints

            # THEN unfulfilled endpoints match
            assert unfulfilled_endpoints == params.unfulfilled_endpoints

        def test_unfulfilled_endpoints_considers_limits(self) -> None:
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

        def test_unfulfilled_endpoints_includes_under_limit(self) -> None:
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

    class TestDependencyGraph:
        @dataclass
        class Params:
            label: str
            bundle: Bundle
            expected_graph_keys: set[str]
            expected_requires: dict[str, set[str]]
            expected_provides: dict[str, set[str]]
            application_dependencies: list[tuple[str, str, bool]]
            endpoint_dependencies: list[tuple[str, str, str, str, bool]]

        test_cases = [
            Params(
                label="no_integrations",
                bundle=Bundle(
                    applications=frozenset(
                        {
                            Application(name="a", charm=sample_charm_pgbouncer_k8s()),
                            Application(name="b", charm=sample_charm_pgbouncer_k8s()),
                        }
                    ),
                    integrations=frozenset(),
                    platform="kubernetes",
                    arch="amd64",
                ),
                expected_graph_keys={"a", "b"},
                expected_requires={"a": set(), "b": set()},
                expected_provides={"a": set(), "b": set()},
                application_dependencies=[("a", "b", False), ("b", "a", False)],
                endpoint_dependencies=[
                    (
                        "a",
                        sample_charm_pgbouncer_k8s().name,
                        sample_charm_endpoint_pgbouncer_k8s_database().name,
                        "requires",
                        False,
                    ),
                    (
                        "b",
                        sample_charm_pgbouncer_k8s().name,
                        sample_charm_endpoint_pgbouncer_k8s_database().name,
                        "requires",
                        False,
                    ),
                ],
            ),
            Params(
                label="simple_chain",
                bundle=Bundle(
                    applications=frozenset(
                        {
                            Application(name="a", charm=sample_charm_pgbouncer_k8s()),
                            Application(name="b", charm=sample_charm_pgbouncer_k8s()),
                            Application(name="c", charm=sample_charm_pgbouncer_k8s()),
                        }
                    ),
                    integrations=frozenset(
                        {
                            Integration(
                                {
                                    ApplicationEndpoint(
                                        "a", sample_charm_endpoint_pgbouncer_k8s_backend_database().name
                                    ),
                                    ApplicationEndpoint("b", sample_charm_endpoint_pgbouncer_k8s_database().name),
                                }
                            ),
                            Integration(
                                {
                                    ApplicationEndpoint(
                                        "b", sample_charm_endpoint_pgbouncer_k8s_backend_database().name
                                    ),
                                    ApplicationEndpoint("c", sample_charm_endpoint_pgbouncer_k8s_database().name),
                                }
                            ),
                        }
                    ),
                    platform="kubernetes",
                    arch="amd64",
                ),
                expected_graph_keys={"a", "b", "c"},
                expected_requires={"a": {"b"}, "b": {"c"}, "c": set()},
                expected_provides={"a": set(), "b": {"a"}, "c": {"b"}},
                application_dependencies=[("a", "b", True), ("b", "c", True), ("a", "c", True)],
                endpoint_dependencies=[
                    (
                        "a",
                        sample_charm_pgbouncer_k8s().name,
                        sample_charm_endpoint_pgbouncer_k8s_backend_database().name,
                        ENDPOINT_REQUIRES,
                        True,
                    ),
                    (
                        "b",
                        sample_charm_pgbouncer_k8s().name,
                        sample_charm_endpoint_pgbouncer_k8s_backend_database().name,
                        ENDPOINT_REQUIRES,
                        True,
                    ),
                    (
                        "c",
                        sample_charm_pgbouncer_k8s().name,
                        sample_charm_endpoint_pgbouncer_k8s_backend_database().name,
                        ENDPOINT_REQUIRES,
                        False,
                    ),
                    (
                        "a",
                        sample_charm_pgbouncer_k8s().name,
                        sample_charm_endpoint_pgbouncer_k8s_database().name,
                        ENDPOINT_PROVIDES,
                        False,
                    ),
                    (
                        "b",
                        sample_charm_pgbouncer_k8s().name,
                        sample_charm_endpoint_pgbouncer_k8s_database().name,
                        ENDPOINT_PROVIDES,
                        True,
                    ),
                    (
                        "c",
                        sample_charm_pgbouncer_k8s().name,
                        sample_charm_endpoint_pgbouncer_k8s_database().name,
                        ENDPOINT_PROVIDES,
                        True,
                    ),
                ],
            ),
            Params(
                label="cycle",
                bundle=Bundle(
                    applications=frozenset(
                        {
                            Application(name="a", charm=sample_charm_pgbouncer_k8s()),
                            Application(name="b", charm=sample_charm_pgbouncer_k8s()),
                        }
                    ),
                    integrations=frozenset(
                        {
                            Integration(
                                {
                                    ApplicationEndpoint("a", sample_charm_endpoint_pgbouncer_k8s_database().name),
                                    ApplicationEndpoint(
                                        "b", sample_charm_endpoint_pgbouncer_k8s_backend_database().name
                                    ),
                                }
                            ),
                            Integration(
                                {
                                    ApplicationEndpoint("b", sample_charm_endpoint_pgbouncer_k8s_database().name),
                                    ApplicationEndpoint(
                                        "a", sample_charm_endpoint_pgbouncer_k8s_backend_database().name
                                    ),
                                }
                            ),
                        }
                    ),
                    platform="kubernetes",
                    arch="amd64",
                ),
                expected_graph_keys={"a", "b"},
                expected_requires={"a": {"b"}, "b": {"a"}},
                expected_provides={"a": {"b"}, "b": {"a"}},
                application_dependencies=[("a", "b", True), ("b", "a", True)],
                endpoint_dependencies=[],
            ),
            Params(
                label="multiple_interfaces",
                bundle=Bundle(
                    applications=frozenset(
                        {
                            Application(name="a", charm=sample_charm_kratos()),
                            Application(name="b", charm=sample_charm_postgresql_k8s()),
                            Application(name="c", charm=sample_charm_self_signed_certificates()),
                        }
                    ),
                    integrations=frozenset(
                        {
                            Integration(
                                {
                                    ApplicationEndpoint("a", sample_charm_endpoint_kratos_pg_database().name),
                                    ApplicationEndpoint("b", sample_charm_endpoint_postgresql_k8s_database().name),
                                }
                            ),
                            Integration(
                                {
                                    ApplicationEndpoint("b", sample_charm_endpoint_postgresql_k8s_certificates().name),
                                    ApplicationEndpoint(
                                        "c", sample_charm_endpoint_self_signed_certificates_certificates().name
                                    ),
                                }
                            ),
                        }
                    ),
                    platform="kubernetes",
                    arch="amd64",
                ),
                expected_graph_keys={"a", "b", "c"},
                expected_requires={"a": {"b"}, "b": {"c"}, "c": set()},
                expected_provides={"a": set(), "b": {"a"}, "c": {"b"}},
                application_dependencies=[("a", "b", True), ("b", "a", False), ("a", "c", True), ("c", "a", False)],
                endpoint_dependencies=[
                    (
                        "a",
                        sample_charm_kratos().name,
                        sample_charm_endpoint_kratos_pg_database().name,
                        ENDPOINT_REQUIRES,
                        True,
                    ),
                    (
                        "a",
                        sample_charm_postgresql_k8s().name,
                        sample_charm_endpoint_postgresql_k8s_database().name,
                        ENDPOINT_PROVIDES,
                        False,
                    ),
                    (
                        "a",
                        sample_charm_postgresql_k8s().name,
                        sample_charm_endpoint_postgresql_k8s_certificates().name,
                        ENDPOINT_REQUIRES,
                        True,
                    ),
                    (
                        "a",
                        sample_charm_self_signed_certificates().name,
                        sample_charm_endpoint_self_signed_certificates_certificates().name,
                        ENDPOINT_PROVIDES,
                        False,
                    ),
                ],
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test_graph_and_dependencies(self, params: Params) -> None:
            # GIVEN a bundle
            bundle = params.bundle

            # WHEN dependency_graph is called
            graph = bundle.dependency_graph

            # THEN graph keys match
            assert set(graph.keys()) == params.expected_graph_keys

            # AND requires/provides match expected
            for app in params.expected_graph_keys:
                assert {dep.application for dep in graph[app].requires} == params.expected_requires[app]
                assert {dep.application for dep in graph[app].provides} == params.expected_provides[app]

            # AND has_application_dependency matches expected
            for dep_app, dep_on_app, expected in params.application_dependencies:
                assert bundle.has_application_dependency(dep_app, dep_on_app) is expected

            # AND has_endpoint_dependency matches expected (if provided)
            for dep_args in params.endpoint_dependencies:
                app, charm, endpoint, typ, expected = dep_args
                assert bundle.has_endpoint_dependency(app, charm, endpoint, typ) is expected

    class TestGenerateUniqueApplicationName:
        @dataclass
        class Params:
            label: str
            applications: set[Application]
            charm: str
            name: str

        test_cases = [
            Params(
                label="charm_does_not_exist",
                applications=set(),
                charm="kratos",
                name="kratos",
            ),
            Params(
                label="instance_of_charm_exists_with_different_name",
                applications={Application("app", sample_charm_kratos())},
                charm="kratos",
                name="kratos",
            ),
            Params(
                label="instance_of_charm_exists",
                applications={Application("kratos", sample_charm_kratos())},
                charm="kratos",
                name="kratos-a",
            ),
            Params(
                label="two_instances_of_charm_exist",
                applications={
                    Application("kratos", sample_charm_kratos()),
                    Application("kratos-a", sample_charm_kratos()),
                },
                charm="kratos",
                name="kratos-b",
            ),
            Params(
                label="two_instances_of_charm_exist_with_skip",
                applications={
                    Application("kratos", sample_charm_kratos()),
                    Application("kratos-b", sample_charm_kratos()),
                },
                charm="kratos",
                name="kratos-a",
            ),
            Params(
                label="over_26_instances_of_charm_exist",
                applications={
                    Application("kratos", sample_charm_kratos()),
                    *{Application(f"kratos-{chr(ord('a') + i)}", sample_charm_kratos()) for i in range(26)},
                    Application("kratos-aa", sample_charm_kratos()),
                },
                charm="kratos",
                name="kratos-ab",
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params) -> None:
            # GIVEN a bundle with the applications
            bundle = dataclasses.replace(
                sample_bundle_postgresql_k8s_kratos(),
                applications=frozenset(params.applications),
            )

            # WHEN a unique name is generated
            name = bundle.generate_unique_application_name(params.charm)

            # THEN matches expected
            assert name == params.name

    def test_export(self) -> None:
        # GIVEN a bundle
        bundle = sample_bundle_postgresql_k8s_kratos()

        # WHEN bundle is exported
        bundle_yaml = bundle.export()

        # THEN matches
        assert yaml.safe_load(bundle_yaml) == {
            "applications": {
                "neighbor": {
                    "base": "ubuntu@24.04",
                    "channel": "edge",
                    "charm": "kratos",
                    "options": {},
                    "revision": 123,
                    "scale": 1,
                    "trust": True,
                },
                "target": {
                    "base": "ubuntu@22.04",
                    "channel": "stable",
                    "charm": "postgresql-k8s",
                    "options": {"config-option": "config-value"},
                    "revision": 1,
                    "scale": 1,
                    "trust": True,
                },
            },
            "bundle": "kubernetes",
            "relations": [["neighbor:pg-database", "target:database"]],
        }

    class TestBundleExportPlatforms:
        @dataclass
        class Params:
            label: str
            platform: str
            expected_scale_key: str
            should_raise_error: bool = False

        test_cases = [
            Params(
                label="kubernetes_platform_uses_scale",
                platform="kubernetes",
                expected_scale_key="scale",
            ),
            Params(
                label="machine_platform_uses_num_units",
                platform="machine",
                expected_scale_key="num_units",
            ),
            Params(
                label="unsupported_platform_raises_error",
                platform="invalid-platform",
                expected_scale_key="",
                should_raise_error=True,
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test_platform_specific_export(self, params: Params) -> None:
            # GIVEN a bundle with specific platform
            bundle = dataclasses.replace(
                sample_bundle_postgresql_k8s_kratos(),
                platform=params.platform,
            )

            if params.should_raise_error:
                # WHEN bundle export is called on unsupported platform
                # THEN ValueError is raised
                with pytest.raises(ValueError, match=f"Unsupported platform: {params.platform}"):
                    bundle.export()
            else:
                # WHEN bundle is exported
                bundle_yaml = bundle.export()
                parsed_yaml = yaml.safe_load(bundle_yaml)

                # THEN the correct scale/unit key is used
                for app_name in parsed_yaml["applications"]:
                    assert params.expected_scale_key in parsed_yaml["applications"][app_name]
                    assert parsed_yaml["applications"][app_name][params.expected_scale_key] == 1

                # AND the platform is correctly set
                assert parsed_yaml["bundle"] == params.platform

        def test_platform_yaml_structure_consistency(self) -> None:
            # GIVEN bundles with different platforms
            kubernetes_bundle = dataclasses.replace(
                sample_bundle_postgresql_k8s_kratos(),
                platform="kubernetes",
            )
            machine_bundle = dataclasses.replace(
                sample_bundle_postgresql_k8s_kratos(),
                platform="machine",
            )

            # WHEN both bundles are exported
            k8s_yaml = yaml.safe_load(kubernetes_bundle.export())
            machine_yaml = yaml.safe_load(machine_bundle.export())

            # THEN structure is identical except for scale/unit key and bundle platform
            assert k8s_yaml["bundle"] == "kubernetes"
            assert machine_yaml["bundle"] == "machine"

            # AND applications have the same structure except for scale/unit key
            k8s_apps = k8s_yaml["applications"]
            machine_apps = machine_yaml["applications"]

            assert set(k8s_apps.keys()) == set(machine_apps.keys())

            for app_name in k8s_apps.keys():
                k8s_app = k8s_apps[app_name].copy()
                machine_app = machine_apps[app_name].copy()

                # Remove scale/unit keys for comparison
                k8s_scale = k8s_app.pop("scale")
                machine_units = machine_app.pop("num_units")

                assert k8s_scale == machine_units == 1
                assert k8s_app == machine_app

    def test_export_mermaid(self):
        # GIVEN a bundle
        bundle = sample_bundle_postgresql_k8s_kratos()

        # WHEN bundle is exported to mermaid
        mermaid = bundle.export_mermaid()

        # THEN mermaid output contains expected structure
        assert mermaid.startswith("graph TB\n")
        assert mermaid.endswith("\n")

        # AND contains application nodes with channel and revision info
        assert 'neighbor["neighbor<br/>(kratos)<br/>edge rev:123"]' in mermaid
        assert 'target["target<br/>(postgresql-k8s)<br/>stable rev:1"]' in mermaid

        # AND contains integration with escaped angle brackets
        assert "target -->|database&lt;db&gt;pg-database| neighbor" in mermaid
