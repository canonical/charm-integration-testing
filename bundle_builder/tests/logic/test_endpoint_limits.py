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
from bundle_builder.overrides import CharmEndpointOverride, CharmMetadataOverride, OverridesClient


class TestEndpointLimits:
    class TestLimitParsing:
        def test_charm_endpoint_override_limit(self):
            # GIVEN an override with limit
            override = CharmEndpointOverride(limit=3)

            # THEN limit is correctly set
            assert override.limit == 3

        def test_charm_endpoint_override_no_limit(self):
            # GIVEN an override without limit
            override = CharmEndpointOverride()

            # THEN limit is None
            assert override.limit is None

    class TestLimitApplication:
        def test_charmhub_client_applies_limit_overrides(self):
            # Mock overrides client that returns limit overrides
            class MockOverridesClient(OverridesClient):
                def get_charm_metadata_overrides(self, charm: str):
                    if charm == "test-charm":
                        return CharmMetadataOverride(provides={"database": CharmEndpointOverride(limit=2)})
                    return CharmMetadataOverride()

            # Mock HTTP client that returns mock charm data
            class MockHttpClient:
                def refresh(self, action):
                    class MockResponse:
                        def __init__(self):
                            self.error = None
                            self.name = "test-charm"
                            self.effective_channel = "stable"

                            class MockCharm:
                                def __init__(self):
                                    self.revision = 1
                                    self.bases = [MockBase()]

                                    class MockMetadata:
                                        def __init__(self):
                                            class MockEndpoint:
                                                def __init__(self, interface, optional=None):
                                                    self.interface = interface
                                                    self.optional = optional

                                            self.provides = {"database": MockEndpoint("postgresql")}
                                            self.requires = {}
                                            self.peers = {}

                                    self.metadata = MockMetadata()

                            self.charm = MockCharm()

                    return MockResponse()

            class MockBase:
                def __init__(self):
                    self.name = "ubuntu"
                    self.architecture = "amd64"
                    self.channel = "22.04"

            # Create CharmhubClient with mock dependencies
            client = CharmhubClient(http_client=MockHttpClient(), overrides_client=MockOverridesClient())

            # WHEN getting charm from store
            charm = client.charm_from_store("test-charm", "amd64")

            # THEN the endpoint has the correct limit
            database_endpoint = next(e for e in charm.endpoints if e.name == "database")
            assert database_endpoint.limit == 2

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
            can_add = builder._can_add_integration(
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
            can_add = builder._can_add_integration(
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
            can_add = builder._can_add_integration(
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

            # THEN the limited endpoint should still be unfulfilled (can accept one more)
            db_endpoint = ApplicationEndpoint(application="db", endpoint="database")
            assert db_endpoint in unfulfilled

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
            can_add = builder._can_add_integration(
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
            can_add = builder._can_add_integration(
                bundle,
                ApplicationEndpoint(application="app1", endpoint="api"),
                ApplicationEndpoint(application="app3", endpoint="upstream"),
            )

            # THEN it should be blocked (app1's endpoint reached limit)
            assert can_add is False
