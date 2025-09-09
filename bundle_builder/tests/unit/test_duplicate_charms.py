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


from unittest.mock import MagicMock

from bundle_builder.bundle import Application, ApplicationEndpoint, Bundle, Integration
from bundle_builder.bundle_builder import BundleBuilder
from bundle_builder.charm import (
    ENDPOINT_PROVIDES,
    ENDPOINT_REQUIRES,
    Charm,
    CharmEndpoint,
    CharmEndpointOptionality,
)


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
        db1_connections = bundle.count_endpoint_connections(
            ApplicationEndpoint(application="postgresql-k8s", endpoint="database")
        )
        db2_connections = bundle.count_endpoint_connections(
            ApplicationEndpoint(application="postgresql-k8s-2", endpoint="database")
        )

        assert db1_connections == 1
        assert db2_connections == 1
