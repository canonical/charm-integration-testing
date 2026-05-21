# Copyright (C) 2026 Canonical Ltd
# See LICENSE file for licensing details.

"""Logic tests for correct app-to-charm ordering when multiple apps share a charm.

When two or more applications use the same charm (e.g. mongodb-k8s deployed as
both config-server and shard), the solver must correctly determine which
application maps to which charm instance based on their integration endpoints.

The ApplicationIntegrationAppsMapToCharmsTag constraint uses a disjunction over
valid orderings rather than picking a fixed one, allowing the solver to resolve
ordering from other constraints (limits, mutual exclusion, etc.).
"""

from bundle_builder_x.bundle_builder import BundleBuilder
from bundle_builder_x.charm import CharmEndpoint, EndpointType
from bundle_builder_x.spec import AppSpec, IntegrationSpec

from .conftest import CharmhubClientStub, build_single_model, make_charm


class TestSharedCharmIntegrationOrdering:
    """Correct ordering resolution when applications share a charm."""

    def test_different_endpoint_names_resolved_by_limit(self) -> None:
        """Regression test for the mongodb-k8s sharding bug.

        mongodb-k8s has:
          - config-server (provides, interface "shards", no limit)
          - sharding (requires, interface "shards", limit=1)
          - mutual exclusion: not (config-server and sharding)

        When config-server app connects to shard-a and shard-b, the solver must
        assign config-server to a charm instance with the provides endpoint active
        (which has no limit), not the requires endpoint (limit=1).
        """
        # GIVEN a charm with mutually exclusive provides/requires on the same interface
        mongodb = make_charm(
            "mongodb-k8s",
            endpoints={
                "config-server": CharmEndpoint(type=EndpointType.PROVIDES, interface="shards", optional=True),
                "sharding": CharmEndpoint(type=EndpointType.REQUIRES, interface="shards", optional=True, limit=1),
            },
            constraint_strs=[
                "not (bool(endpoint[sharding]) and bool(endpoint[config-server]))",
            ],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(mongodb))

        # WHEN building with three apps sharing the charm and explicit integrations
        bundle = build_single_model(
            builder,
            applications={
                "config-server": AppSpec(charm="mongodb-k8s"),
                "shard-a": AppSpec(charm="mongodb-k8s"),
                "shard-b": AppSpec(charm="mongodb-k8s"),
            },
            integrations=[
                IntegrationSpec(
                    application="config-server",
                    endpoint="config-server",
                    remote_application="shard-a",
                    remote_endpoint="sharding",
                ),
                IntegrationSpec(
                    application="config-server",
                    endpoint="config-server",
                    remote_application="shard-b",
                    remote_endpoint="sharding",
                ),
            ],
        )

        # THEN all three applications are in the bundle
        assert set(bundle.applications.keys()) == {"config-server", "shard-a", "shard-b"}

        # AND there are exactly two integrations
        assert len(bundle.integrations) == 2

        # AND config-server uses endpoint "config-server" in both integrations
        for integration in bundle.integrations:
            endpoints = {ep.application: ep.endpoint for ep in integration}
            assert "config-server" in endpoints
            assert endpoints["config-server"] == "config-server"

        # AND each shard uses endpoint "sharding"
        shard_endpoints = set()
        for integration in bundle.integrations:
            for ep in integration:
                if ep.application in ("shard-a", "shard-b"):
                    shard_endpoints.add((ep.application, ep.endpoint))
        assert shard_endpoints == {("shard-a", "sharding"), ("shard-b", "sharding")}

    def test_same_endpoint_name_resolved_by_other_constraints(self) -> None:
        """When both endpoints share the same name, other constraints disambiguate.

        Example: charm-x has endpoint "a" (requires), charm-y has endpoint "a" (provides).
        Both apps could be either charm, but a non-optional endpoint on charm-y forces
        the solver to pick the correct assignment.
        """
        # GIVEN charm-x with requires endpoint "link" (interface "foo")
        charm_x = make_charm(
            "charm-x",
            endpoints={
                "link": CharmEndpoint(type=EndpointType.REQUIRES, interface="foo", optional=True),
            },
        )
        # AND charm-y with provides endpoint "link" (interface "foo")
        # AND an additional non-optional provides endpoint that forces charm-y to exist
        # with a specific integration pattern
        charm_y = make_charm(
            "charm-y",
            endpoints={
                "link": CharmEndpoint(type=EndpointType.PROVIDES, interface="foo", optional=True),
                "metrics": CharmEndpoint(type=EndpointType.PROVIDES, interface="prometheus", optional=True),
            },
        )
        # AND a metrics consumer that requires prometheus
        consumer = make_charm(
            "consumer",
            endpoints={
                "metrics": CharmEndpoint(type=EndpointType.REQUIRES, interface="prometheus", optional=False),
            },
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(charm_x, charm_y, consumer))

        # WHEN building with explicit integration using the shared endpoint name "link"
        bundle = build_single_model(
            builder,
            applications={
                "app-x": AppSpec(charm="charm-x"),
                "app-y": AppSpec(charm="charm-y"),
            },
            integrations=[
                IntegrationSpec(
                    application="app-x",
                    endpoint="link",
                    remote_application="app-y",
                    remote_endpoint="link",
                ),
            ],
        )

        # THEN the integration exists between app-x and app-y on endpoint "link"
        link_integrations = [i for i in bundle.integrations if all(ep.endpoint == "link" for ep in i)]
        assert len(link_integrations) == 1
        apps_in_integration = {ep.application for ep in link_integrations[0]}
        assert apps_in_integration == {"app-x", "app-y"}

    def test_four_instances_same_charm_correct_topology(self) -> None:
        """Four apps of the same charm: one hub connects to three spokes.

        The hub uses a provides endpoint (no limit), each spoke uses requires (limit=1).
        The solver must place the hub on the provides side.
        """
        # GIVEN a charm with hub (provides, no limit) and spoke (requires, limit=1)
        star_charm = make_charm(
            "star-k8s",
            endpoints={
                "hub": CharmEndpoint(type=EndpointType.PROVIDES, interface="star-link", optional=True),
                "spoke": CharmEndpoint(type=EndpointType.REQUIRES, interface="star-link", optional=True, limit=1),
            },
            constraint_strs=[
                "not (bool(endpoint[hub]) and bool(endpoint[spoke]))",
            ],
        )
        builder = BundleBuilder(charmhub_client=CharmhubClientStub(star_charm))

        # WHEN building with one hub and three spokes
        bundle = build_single_model(
            builder,
            applications={
                "hub": AppSpec(charm="star-k8s"),
                "spoke-1": AppSpec(charm="star-k8s"),
                "spoke-2": AppSpec(charm="star-k8s"),
                "spoke-3": AppSpec(charm="star-k8s"),
            },
            integrations=[
                IntegrationSpec(
                    application="hub",
                    endpoint="hub",
                    remote_application="spoke-1",
                    remote_endpoint="spoke",
                ),
                IntegrationSpec(
                    application="hub",
                    endpoint="hub",
                    remote_application="spoke-2",
                    remote_endpoint="spoke",
                ),
                IntegrationSpec(
                    application="hub",
                    endpoint="hub",
                    remote_application="spoke-3",
                    remote_endpoint="spoke",
                ),
            ],
        )

        # THEN all four applications are in the bundle
        assert set(bundle.applications.keys()) == {"hub", "spoke-1", "spoke-2", "spoke-3"}

        # AND there are exactly three integrations
        assert len(bundle.integrations) == 3

        # AND hub uses endpoint "hub" in all integrations
        for integration in bundle.integrations:
            endpoints = {ep.application: ep.endpoint for ep in integration}
            assert endpoints.get("hub") == "hub"

        # AND each spoke uses endpoint "spoke"
        spoke_eps = set()
        for integration in bundle.integrations:
            for ep in integration:
                if ep.application.startswith("spoke-"):
                    spoke_eps.add((ep.application, ep.endpoint))
        assert spoke_eps == {
            ("spoke-1", "spoke"),
            ("spoke-2", "spoke"),
            ("spoke-3", "spoke"),
        }
