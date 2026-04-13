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

import pytest

from bundle_builder.bundle import Application, ApplicationEndpoint, Bundle, Integration
from bundle_builder.bundle_builder import BundleBuilder, UnfulfilledEndpointsError
from bundle_builder.charm import (
    ENDPOINT_PROVIDES,
    ENDPOINT_REQUIRES,
    Charm,
    CharmChannel,
    CharmEndpoint,
    CharmEndpointOptionality,
    CharmLimit,
)
from bundle_builder.juju_version import JujuVersion

from .conftest import CharmhubClientStub


class TestDependencyCycle:
    def test_charm_self_loop(self) -> None:
        # GIVEN a charm that provides and requires the same interface
        provides_and_requires_same_interface_charm = Charm(
            name="charm-a",
            channel=CharmChannel("stable"),
            revision=1,
            ubuntu_version="22.04",
            ubuntu_arch="amd64",
            endpoints=frozenset(
                {
                    CharmEndpoint(
                        type=ENDPOINT_PROVIDES,
                        name="interface-provider",
                        interface="some-interface",
                        optionality=CharmEndpointOptionality.from_bool(True),
                        limits=(),
                    ),
                    CharmEndpoint(
                        type=ENDPOINT_REQUIRES,
                        name="interface-consumer",
                        interface="some-interface",
                        optionality=CharmEndpointOptionality.from_bool(False),
                        limits=(),
                    ),
                },
            ),
            priority=1.0,
        )
        # AND a base bundle with an easy to make cycle
        bundle = Bundle(
            applications=frozenset(
                {
                    Application(name="application-a", charm=provides_and_requires_same_interface_charm),
                }
            ),
            integrations=frozenset(),
            platform="machine",
            arch="amd64",
            juju_version=JujuVersion.parse("3.6"),
        )
        # AND a bundle builder with a charmhub client that knows about the charm
        builder = BundleBuilder(CharmhubClientStub(provides_and_requires_same_interface_charm))

        # WHEN we build the bundle
        # THEN it errors because of unfulfilled endpoints
        with pytest.raises(UnfulfilledEndpointsError) as caught:
            builder.build(bundle)

        # AND the added charm must have unfulfilled non-optional endpoint
        #   because we don't allow a loop back
        #   between the existing application-a and the charm-a added by the bundle-builder
        assert caught.value.unfulfilled_endpoints == {ApplicationEndpoint("charm-a", "interface-consumer")}

        new_bundle = caught.value.best_bundle

        # AND in the last best bundle, charm should be added once
        assert len(new_bundle.applications) == 2
        # AND the integration exists once
        assert new_bundle.integrations == {
            Integration(
                {
                    ApplicationEndpoint("application-a", "interface-consumer"),
                    ApplicationEndpoint("charm-a", "interface-provider"),
                }
            ),
        }

    def test_charm_self_loop_provides(self) -> None:
        # GIVEN a charm that provides and requires the same interface
        provides_and_requires_same_interface_charm = Charm(
            name="charm-a",
            channel=CharmChannel("stable"),
            revision=1,
            ubuntu_version="22.04",
            ubuntu_arch="amd64",
            endpoints=frozenset(
                {
                    CharmEndpoint(
                        type=ENDPOINT_PROVIDES,
                        name="interface-provider",
                        interface="some-interface",
                        optionality=CharmEndpointOptionality.from_bool(False),
                        limits=(),
                    ),
                    CharmEndpoint(
                        type=ENDPOINT_REQUIRES,
                        name="interface-consumer",
                        interface="some-interface",
                        optionality=CharmEndpointOptionality.from_bool(True),
                        limits=(),
                    ),
                },
            ),
            priority=1.0,
        )
        # AND a base bundle with an easy to make cycle
        bundle = Bundle(
            applications=frozenset(
                {
                    Application(name="application-a", charm=provides_and_requires_same_interface_charm),
                }
            ),
            integrations=frozenset(),
            platform="machine",
            arch="amd64",
            juju_version=JujuVersion.parse("3.6"),
        )
        # AND a bundle builder with a charmhub client that knows about the charm
        builder = BundleBuilder(CharmhubClientStub(provides_and_requires_same_interface_charm))

        # WHEN we build the bundle
        # THEN it errors because of unfulfilled endpoints
        with pytest.raises(UnfulfilledEndpointsError) as caught:
            builder.build(bundle)

        # AND the added charm must have unfulfilled non-optional endpoint
        #   because we don't allow a loop back
        #   between the existing application-a and the charm-a added by the bundle-builder
        assert caught.value.unfulfilled_endpoints == {ApplicationEndpoint("charm-a", "interface-provider")}

        new_bundle = caught.value.best_bundle

        # AND in the last best bundle, the charm should be added once
        assert len(new_bundle.applications) == 2
        # AND the integration exists once
        assert new_bundle.integrations == {
            Integration(
                {
                    ApplicationEndpoint("application-a", "interface-provider"),
                    ApplicationEndpoint("charm-a", "interface-consumer"),
                }
            ),
        }

    def test_multiple_charms_provided(self) -> None:
        # GIVEN a charm that provides and requires the same interface
        charm_a = Charm(
            name="charm-a",
            channel=CharmChannel("stable"),
            revision=1,
            ubuntu_version="22.04",
            ubuntu_arch="amd64",
            endpoints=frozenset(
                {
                    CharmEndpoint(
                        type=ENDPOINT_PROVIDES,
                        name="interface-provider",
                        interface="some-interface",
                        optionality=CharmEndpointOptionality.from_bool(True),
                        limits=(CharmLimit(limit=1),),
                    ),
                    CharmEndpoint(
                        type=ENDPOINT_REQUIRES,
                        name="interface-consumer",
                        interface="some-interface",
                        optionality=CharmEndpointOptionality.from_bool(False),
                        limits=(CharmLimit(limit=1),),
                    ),
                },
            ),
            priority=1.0,
        )
        # AND a second charm that only provides the interface
        charm_b = Charm(
            name="charm-b",
            channel=CharmChannel("stable"),
            revision=1,
            ubuntu_version="22.04",
            ubuntu_arch="amd64",
            endpoints=frozenset(
                {
                    CharmEndpoint(
                        type=ENDPOINT_PROVIDES,
                        name="interface-provider",
                        interface="some-interface",
                        optionality=CharmEndpointOptionality.from_bool(True),
                        limits=(CharmLimit(limit=1),),
                    ),
                },
            ),
            priority=1.0,
        )
        # AND a base bundle with several of the recursively dependent charms
        bundle = Bundle(
            applications=frozenset(
                {
                    Application(name="application-a", charm=charm_a),
                    Application(name="application-b", charm=charm_a),
                    Application(name="application-c", charm=charm_a),
                }
            ),
            integrations=frozenset(),
            platform="machine",
            arch="amd64",
            juju_version=JujuVersion.parse("3.6"),
        )
        # AND a bundle builder with a charmhub client that knows about the charms
        builder = BundleBuilder(CharmhubClientStub(charm_a, charm_b))

        # WHEN we build the bundle
        new_bundle = builder.build(bundle)

        # THEN the three applications should be integrated in a chain
        assert {
            ApplicationEndpoint("application-a", "interface-consumer"),
            ApplicationEndpoint("application-b", "interface-consumer"),
            ApplicationEndpoint("application-c", "interface-consumer"),
        } <= {endpoint for integration in new_bundle.integrations for endpoint in integration}
        # AND the providing charm provides the integration
        assert ApplicationEndpoint("charm-b", "interface-provider") in {
            endpoint for integration in new_bundle.integrations for endpoint in integration
        }

    def test_multiple_charms_dependency_chain(self) -> None:
        # GIVEN a charm that provides and requires some interface
        charm_a = Charm(
            name="charm-a",
            channel=CharmChannel("stable"),
            revision=1,
            ubuntu_version="22.04",
            ubuntu_arch="amd64",
            endpoints=frozenset(
                {
                    CharmEndpoint(
                        type=ENDPOINT_PROVIDES,
                        name="interface-provider",
                        interface="some-interface-a",
                        optionality=CharmEndpointOptionality.from_bool(True),
                        limits=(CharmLimit(limit=1),),
                    ),
                    CharmEndpoint(
                        type=ENDPOINT_REQUIRES,
                        name="interface-consumer",
                        interface="some-interface-b",
                        optionality=CharmEndpointOptionality.from_bool(False),
                        limits=(CharmLimit(limit=1),),
                    ),
                },
            ),
            priority=1.0,
        )
        # AND a second charm that provides and requires the opposite interfaces
        charm_b = Charm(
            name="charm-b",
            channel=CharmChannel("stable"),
            revision=1,
            ubuntu_version="22.04",
            ubuntu_arch="amd64",
            endpoints=frozenset(
                {
                    CharmEndpoint(
                        type=ENDPOINT_PROVIDES,
                        name="interface-provider",
                        interface="some-interface-b",
                        optionality=CharmEndpointOptionality.from_bool(True),
                        limits=(CharmLimit(limit=1),),
                    ),
                    CharmEndpoint(
                        type=ENDPOINT_REQUIRES,
                        name="interface-consumer",
                        interface="some-interface-a",
                        optionality=CharmEndpointOptionality.from_bool(False),
                        limits=(CharmLimit(limit=1),),
                    ),
                },
            ),
            priority=1.0,
        )
        # AND a base bundle with a charm
        bundle = Bundle(
            applications=frozenset(
                {
                    Application(name="application-a", charm=charm_a),
                }
            ),
            integrations=frozenset(),
            platform="machine",
            arch="amd64",
            juju_version=JujuVersion.parse("3.6"),
        )
        # AND a bundle builder with a charmhub client that knows about the charms
        builder = BundleBuilder(CharmhubClientStub(charm_a, charm_b), avoid_application_dependency_cycles=True)

        # WHEN we build the bundle
        # THEN it errors because of unfulfilled endpoints
        with pytest.raises(UnfulfilledEndpointsError) as caught:
            builder.build(bundle)

        # AND the added charm-a must have unfulfilled non-optional endpoint
        #   because we don't allow a loop back
        #   between the existing application-a and the charm-a added by the bundle-builder, nor
        #   between the charm-b and charm-a added by the bundle-builder
        assert caught.value.unfulfilled_endpoints == {ApplicationEndpoint("charm-a", "interface-consumer")}

        new_bundle = caught.value.best_bundle

        # AND in the last best bundle, the given charm should be integrated with the second charm
        assert (
            Integration(
                {
                    ApplicationEndpoint("application-a", "interface-consumer"),
                    ApplicationEndpoint("charm-b", "interface-provider"),
                }
            )
            in new_bundle.integrations
        )
        # AND the second charm is integrated with another instance of the first charm
        assert (
            Integration(
                {
                    ApplicationEndpoint("charm-b", "interface-consumer"),
                    ApplicationEndpoint("charm-a", "interface-provider"),
                }
            )
            in new_bundle.integrations
        )
        # AND there are no more integrations
        assert len(new_bundle.integrations) == 2
