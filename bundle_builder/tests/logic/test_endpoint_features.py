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

from .conftest import CharmhubClientStub


class TestEndpointFeatures:
    def test_provider_features_enable_optional_requirer_endpoint(self):
        # GIVEN a provider charm with SSL feature
        provider_with_ssl = Charm(
            name="database",
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
                        limits=(),
                        features=frozenset({"ssl", "compression"}),
                    )
                }
            ),
            priority=1.0,
        )

        # AND a requirer charm with an optional endpoint that requires SSL feature
        requirer_with_ssl_requirement = Charm(
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
                        limits=(),
                        features=frozenset({"ssl"}),
                    ),
                    CharmEndpoint(
                        type=ENDPOINT_REQUIRES,
                        name="monitoring",
                        interface="prometheus",
                        # Optional only if database endpoint has SSL feature
                        optionality=CharmEndpointOptionality(endpoint_feature="database:ssl"),
                        limits=(),
                    ),
                }
            ),
            priority=1.0,
        )

        # AND a monitoring provider
        monitoring_provider = Charm(
            name="prometheus",
            channel="stable",
            revision=1,
            ubuntu_version="22.04",
            ubuntu_arch="amd64",
            endpoints=frozenset(
                {
                    CharmEndpoint(
                        type=ENDPOINT_PROVIDES,
                        name="metrics",
                        interface="prometheus",
                        optionality=CharmEndpointOptionality.from_bool(True),
                        limits=(),
                    )
                }
            ),
            priority=1.0,
        )

        # AND a bundle with app and database connected
        bundle = Bundle(
            applications=frozenset(
                {
                    Application(name="db", charm=provider_with_ssl),
                    Application(name="app", charm=requirer_with_ssl_requirement),
                }
            ),
            integrations=frozenset(
                {
                    Integration(
                        {
                            ApplicationEndpoint("db", "database"),
                            ApplicationEndpoint("app", "database"),
                        }
                    )
                }
            ),
            platform="machine",
            arch="amd64",
        )

        # AND a bundle builder
        builder = BundleBuilder(
            CharmhubClientStub(provider_with_ssl, requirer_with_ssl_requirement, monitoring_provider)
        )

        # WHEN we build the bundle
        new_bundle = builder.build(bundle)

        # THEN monitoring endpoint should be optional (SSL feature present)
        # so it should not be in unfulfilled endpoints
        monitoring_endpoint = ApplicationEndpoint("app", "monitoring")
        assert monitoring_endpoint not in new_bundle.unfulfilled_endpoints

    def test_provider_without_required_feature_makes_endpoint_required(self):
        # GIVEN a provider charm WITHOUT SSL feature
        provider_without_ssl = Charm(
            name="database",
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
                        limits=(),
                        features=frozenset({"compression"}),  # No SSL
                    )
                }
            ),
            priority=1.0,
        )

        # AND a requirer charm with an optional endpoint that requires SSL feature
        requirer_with_ssl_requirement = Charm(
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
                        limits=(),
                        features=frozenset(),  # No features required
                    ),
                    CharmEndpoint(
                        type=ENDPOINT_REQUIRES,
                        name="monitoring",
                        interface="prometheus",
                        # Optional only if database endpoint has SSL feature
                        optionality=CharmEndpointOptionality(endpoint_feature="database:ssl"),
                        limits=(),
                    ),
                }
            ),
            priority=1.0,
        )

        # AND a monitoring provider
        monitoring_provider = Charm(
            name="prometheus",
            channel="stable",
            revision=1,
            ubuntu_version="22.04",
            ubuntu_arch="amd64",
            endpoints=frozenset(
                {
                    CharmEndpoint(
                        type=ENDPOINT_PROVIDES,
                        name="metrics",
                        interface="prometheus",
                        optionality=CharmEndpointOptionality.from_bool(True),
                        limits=(),
                    )
                }
            ),
            priority=1.0,
        )

        # AND a bundle with app and database connected
        bundle = Bundle(
            applications=frozenset(
                {
                    Application(name="db", charm=provider_without_ssl),
                    Application(name="app", charm=requirer_with_ssl_requirement),
                }
            ),
            integrations=frozenset(
                {
                    Integration(
                        {
                            ApplicationEndpoint("db", "database"),
                            ApplicationEndpoint("app", "database"),
                        }
                    )
                }
            ),
            platform="machine",
            arch="amd64",
        )

        # AND a bundle builder
        builder = BundleBuilder(
            CharmhubClientStub(provider_without_ssl, requirer_with_ssl_requirement, monitoring_provider)
        )

        # WHEN we build the bundle
        new_bundle = builder.build(bundle)

        # THEN monitoring endpoint should NOT be optional (SSL feature missing)
        # so it SHOULD be in unfulfilled endpoints and get fulfilled
        monitoring_endpoint = ApplicationEndpoint("app", "monitoring")
        # The builder should add prometheus to fulfill the monitoring endpoint
        assert any(app.charm.name == "prometheus" for app in new_bundle.applications)
        # And the monitoring endpoint should be integrated
        assert any(monitoring_endpoint in integration for integration in new_bundle.integrations)

    def test_multiple_requirers_with_different_features(self):
        # GIVEN a provider charm with multiple features
        provider = Charm(
            name="database",
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
                        limits=(),
                        features=frozenset({"ssl", "compression", "replication"}),
                    )
                }
            ),
            priority=1.0,
        )

        # AND two requirer charms with different feature requirements
        requirer1 = Charm(
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
                        limits=(),
                        features=frozenset({"ssl"}),
                    )
                }
            ),
            priority=1.0,
        )

        requirer2 = Charm(
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
                        limits=(),
                        features=frozenset({"compression", "replication"}),
                    )
                }
            ),
            priority=1.0,
        )

        # AND a bundle with all three connected
        bundle = Bundle(
            applications=frozenset(
                {
                    Application(name="db", charm=provider),
                    Application(name="app1", charm=requirer1),
                    Application(name="app2", charm=requirer2),
                }
            ),
            integrations=frozenset(
                {
                    Integration(
                        {
                            ApplicationEndpoint("db", "database"),
                            ApplicationEndpoint("app1", "database"),
                        }
                    ),
                    Integration(
                        {
                            ApplicationEndpoint("db", "database"),
                            ApplicationEndpoint("app2", "database"),
                        }
                    ),
                }
            ),
            platform="machine",
            arch="amd64",
        )

        # AND a bundle builder
        builder = BundleBuilder(CharmhubClientStub(provider, requirer1, requirer2))

        # WHEN we build the bundle
        new_bundle = builder.build(bundle)

        # THEN the provider should have all features from both requirers
        features = new_bundle.application_endpoint_features("db")
        assert "database:ssl" in features
        assert "database:compression" in features
        assert "database:replication" in features

        # AND requirer1 should have only its features
        features1 = new_bundle.application_endpoint_features("app1")
        assert "database:ssl" in features1
        assert "database:compression" not in features1

        # AND requirer2 should have only its features
        features2 = new_bundle.application_endpoint_features("app2")
        assert "database:compression" in features2
        assert "database:replication" in features2
        assert "database:ssl" not in features2

    def test_feature_based_optionality_with_complex_conditions(self):
        # GIVEN a provider with features
        provider = Charm(
            name="database",
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
                        limits=(),
                        features=frozenset({"ssl", "backup"}),
                    )
                }
            ),
            priority=1.0,
        )

        # AND a requirer with complex optionality (requires both SSL AND backup features)
        requirer = Charm(
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
                        limits=(),
                        features=frozenset({"ssl", "backup"}),
                    ),
                    CharmEndpoint(
                        type=ENDPOINT_REQUIRES,
                        name="logging",
                        interface="syslog",
                        # Optional only if database has BOTH ssl AND backup features
                        optionality=CharmEndpointOptionality(
                            all_of=frozenset(
                                {
                                    CharmEndpointOptionality(endpoint_feature="database:ssl"),
                                    CharmEndpointOptionality(endpoint_feature="database:backup"),
                                }
                            )
                        ),
                        limits=(),
                    ),
                }
            ),
            priority=1.0,
        )

        logger = Charm(
            name="logger",
            channel="stable",
            revision=1,
            ubuntu_version="22.04",
            ubuntu_arch="amd64",
            endpoints=frozenset(
                {
                    CharmEndpoint(
                        type=ENDPOINT_PROVIDES,
                        name="syslog",
                        interface="syslog",
                        optionality=CharmEndpointOptionality.from_bool(True),
                        limits=(),
                    )
                }
            ),
            priority=1.0,
        )

        # AND a bundle with app and database connected
        bundle = Bundle(
            applications=frozenset(
                {
                    Application(name="db", charm=provider),
                    Application(name="app", charm=requirer),
                }
            ),
            integrations=frozenset(
                {
                    Integration(
                        {
                            ApplicationEndpoint("db", "database"),
                            ApplicationEndpoint("app", "database"),
                        }
                    )
                }
            ),
            platform="machine",
            arch="amd64",
        )

        # AND a bundle builder
        builder = BundleBuilder(CharmhubClientStub(provider, requirer, logger))

        # WHEN we build the bundle
        new_bundle = builder.build(bundle)

        # THEN logging endpoint should be optional (both features present)
        logging_endpoint = ApplicationEndpoint("app", "logging")
        assert logging_endpoint not in new_bundle.unfulfilled_endpoints
        # And no logger should be added
        assert not any(app.charm.name == "logger" for app in new_bundle.applications)
