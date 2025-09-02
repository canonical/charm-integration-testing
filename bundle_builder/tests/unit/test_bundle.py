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
        def test_platform_specific_export(self, params: Params):
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

        def test_platform_yaml_structure_consistency(self):
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


class TestBundleEndpointConnectionCounts:
    def test_no_connections(self):
        # GIVEN a bundle with no integrations
        bundle = Bundle(
            applications=frozenset({
                Application(name="postgresql-k8s", charm=sample_charm_postgresql_k8s()),
                Application(name="kratos", charm=sample_charm_kratos()),
            }),
            integrations=frozenset(),
            platform="kubernetes",
            arch="amd64",
        )

        # WHEN connection counts are computed
        counts = bundle.endpoint_connection_counts

        # THEN all endpoints have zero connections
        assert counts == {}

    def test_single_integration(self):
        # GIVEN a bundle with one integration
        bundle = sample_bundle_postgresql_k8s_kratos()

        # WHEN connection counts are computed
        counts = bundle.endpoint_connection_counts

        # THEN both endpoints have count=1
        expected_counts = {
            ApplicationEndpoint(application="target", endpoint="database"): 1,
            ApplicationEndpoint(application="neighbor", endpoint="pg-database"): 1,
        }
        assert counts == expected_counts

    def test_multiple_integrations_same_endpoint(self):
        # GIVEN a bundle with multiple integrations involving same endpoint
        postgresql_app = Application(name="postgresql-k8s", charm=sample_charm_postgresql_k8s())
        kratos1_app = Application(name="kratos1", charm=sample_charm_kratos())
        kratos2_app = Application(name="kratos2", charm=sample_charm_kratos())

        bundle = Bundle(
            applications=frozenset({postgresql_app, kratos1_app, kratos2_app}),
            integrations=frozenset({
                Integration({
                    ApplicationEndpoint(application="postgresql-k8s", endpoint="database"),
                    ApplicationEndpoint(application="kratos1", endpoint="pg-database"),
                }),
                Integration({
                    ApplicationEndpoint(application="postgresql-k8s", endpoint="database"),
                    ApplicationEndpoint(application="kratos2", endpoint="pg-database"),
                }),
            }),
            platform="kubernetes",
            arch="amd64",
        )

        # WHEN connection counts are computed
        counts = bundle.endpoint_connection_counts

        # THEN postgresql-k8s:database has count=2, others have count=1
        expected_counts = {
            ApplicationEndpoint(application="postgresql-k8s", endpoint="database"): 2,
            ApplicationEndpoint(application="kratos1", endpoint="pg-database"): 1,
            ApplicationEndpoint(application="kratos2", endpoint="pg-database"): 1,
        }
        assert counts == expected_counts

    def test_complex_multiple_integrations(self):
        # GIVEN a bundle with mixed integration patterns
        postgresql_app = Application(name="postgresql-k8s", charm=sample_charm_postgresql_k8s())
        kratos1_app = Application(name="kratos1", charm=sample_charm_kratos())
        kratos2_app = Application(name="kratos2", charm=sample_charm_kratos())
        self_signed_app = Application(name="self-signed-certificates", charm=sample_charm_self_signed_certificates())

        bundle = Bundle(
            applications=frozenset({postgresql_app, kratos1_app, kratos2_app, self_signed_app}),
            integrations=frozenset({
                # Database connections: postgresql <-> both kratos instances
                Integration({
                    ApplicationEndpoint(application="postgresql-k8s", endpoint="database"),
                    ApplicationEndpoint(application="kratos1", endpoint="pg-database"),
                }),
                Integration({
                    ApplicationEndpoint(application="postgresql-k8s", endpoint="database"),
                    ApplicationEndpoint(application="kratos2", endpoint="pg-database"),
                }),
                # Certificates: self-signed <-> postgresql
                Integration({
                    ApplicationEndpoint(application="self-signed-certificates", endpoint="certificates"),
                    ApplicationEndpoint(application="postgresql-k8s", endpoint="certificates"),
                }),
            }),
            platform="kubernetes",
            arch="amd64",
        )

        # WHEN connection counts are computed
        counts = bundle.endpoint_connection_counts

        # THEN postgresql database endpoint has count=2, others have count=1
        expected_counts = {
            ApplicationEndpoint(application="postgresql-k8s", endpoint="database"): 2,
            ApplicationEndpoint(application="postgresql-k8s", endpoint="certificates"): 1,
            ApplicationEndpoint(application="kratos1", endpoint="pg-database"): 1,
            ApplicationEndpoint(application="kratos2", endpoint="pg-database"): 1,
            ApplicationEndpoint(application="self-signed-certificates", endpoint="certificates"): 1,
        }
        assert counts == expected_counts
