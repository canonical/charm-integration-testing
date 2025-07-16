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

import pytest
import yaml
from pydantic.dataclasses import dataclass

from bundle_builder.bundle import Application, ApplicationEndpoint, Bundle, Integration

from .test_charm import (
    sample_charm_endpoint_kratos_pg_database,
    sample_charm_endpoint_postgresql_k8s_certificates,
    sample_charm_endpoint_postgresql_k8s_database,
    sample_charm_kratos,
    sample_charm_postgresql_k8s,
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
        def test(self, params: Params):
            # GIVEN the application
            application = params.application

            # WHEN repr is called
            repr = application.__repr__()

            # THEN matches expected
            assert repr == params.repr


class TestApplicationEndpoint:
    sample_application_endpoint = ApplicationEndpoint("postgresql-k8s", "certificates")

    def test_repr(self):
        # GIVEN an application endpoint
        application_endpoint = self.sample_application_endpoint

        # WHEN repr is called
        repr = application_endpoint.__repr__()

        # THEN repr is application:endpoint
        assert repr == f"{application_endpoint.application}:{application_endpoint.endpoint}"

    def test_str(self):
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
                        ApplicationEndpoint("target", "database"),
                        ApplicationEndpoint("neighbor", "pg-database"),
                    }
                )
            }
        ),
        platform="kubernetes",
        arch="amd64",
    )


class TestBundle:
    def test_application_endpoints(self):
        # GIVEN a bundle
        bundle = sample_bundle_postgresql_k8s_kratos()

        # WHEN the application endpoints property is fetched
        application_endpoints = bundle.application_endpoints

        # THEN matches
        assert application_endpoints == {
            ApplicationEndpoint("target", "certificates"): sample_charm_endpoint_postgresql_k8s_certificates(),
            ApplicationEndpoint("target", "database"): sample_charm_endpoint_postgresql_k8s_database(),
            ApplicationEndpoint("neighbor", "pg-database"): sample_charm_endpoint_kratos_pg_database(),
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
        def test(self, params: Params):
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
                unfulfilled_endpoints=frozenset({ApplicationEndpoint("neighbor", "pg-database")}),
            ),
            Params(
                label="non_optional_endpoint_fulfilled",
                bundle=sample_bundle_postgresql_k8s_kratos(),
                unfulfilled_endpoints=frozenset(),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params):
            # GIVEN the bundle
            bundle = params.bundle

            # WHEN unfulfilled endpoints is called
            unfulfilled_endpoints = bundle.unfulfilled_endpoints

            # THEN unfulfilled endpoints match
            assert unfulfilled_endpoints == params.unfulfilled_endpoints

    class TestUnfulfilledInterfaces:
        @dataclass
        class Params:
            label: str
            bundle: Bundle
            unfulfilled_interfaces: set[str]

        test_cases = [
            Params(
                label="no_unfulfilled_interfaces",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    applications=frozenset(
                        {
                            Application(name="target", charm=sample_charm_postgresql_k8s()),
                        }
                    ),
                    integrations=frozenset(),
                ),
                unfulfilled_interfaces=frozenset(),
            ),
            Params(
                label="unfulfilled_interface",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    applications=frozenset(
                        {
                            Application(name="neighbor", charm=sample_charm_kratos()),
                        }
                    ),
                    integrations=frozenset(),
                ),
                unfulfilled_interfaces=frozenset({"db"}),
            ),
            Params(
                label="multiple_same_unfulfilled_interface",
                bundle=dataclasses.replace(
                    sample_bundle_postgresql_k8s_kratos(),
                    applications=frozenset(
                        {
                            Application(name="kratos-1", charm=sample_charm_kratos()),
                            Application(name="kratos-2", charm=sample_charm_kratos()),
                        }
                    ),
                    integrations=frozenset(),
                ),
                unfulfilled_interfaces=frozenset({"db"}),
            ),
        ]

        @pytest.mark.parametrize("params", test_cases, ids=[params.label for params in test_cases])
        def test(self, params: Params):
            # GIVEN the bundle
            bundle = params.bundle

            # WHEN unfulfilled interfaces is called
            unfulfilled_interfaces = bundle.unfulfilled_interfaces

            # THEN unfulfilled endpoints match
            assert unfulfilled_interfaces == params.unfulfilled_interfaces

    def test_export(self):
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
