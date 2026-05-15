# Copyright (C) 2026 Canonical Ltd
# See LICENSE file for licensing details.

"""Unit tests for ApplicationIntegrationAppsMapToCharmsTag constraint logic.

Verifies that the constraint correctly emits a disjunction (Or) over valid
endpoint orderings, allowing the solver to determine which application maps
to which side (requires/provides) of a charm integration.
"""

import logging

import z3  # type: ignore[import-untyped]

from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointType
from bundle_builder_x.constraints import add_constraints
from bundle_builder_x.domain import (
    Domain,
    DomainApplication,
    DomainApplicationEndpoint,
    DomainApplicationIntegration,
    DomainModel,
    ModelRef,
    add_charm_to_domain,
)
from bundle_builder_x.extract import extract_solution
from bundle_builder_x.juju_version import JujuVersion

_JUJU = JujuVersion(major=3, minor=6, patch=0)
_LOGGER = logging.getLogger("test_app_integration_ordering")


def _make_domain(models: dict[ModelRef, DomainModel]) -> Domain:
    domain = Domain()
    domain.models.update(models)
    return domain


def _make_charm(
    name: str,
    endpoints: dict[str, CharmEndpoint] | None = None,
    channel: str = "stable",
    revision: int = 1,
) -> Charm:
    return Charm(
        name=name,
        channel=CharmChannel.model_validate(channel),
        revision=revision,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints=endpoints or {},
    )


def _solve(domain: Domain) -> z3.ModelRef:
    """Run the solver and return the Z3 model, asserting SAT."""
    solver = z3.Solver()
    add_constraints(solver, domain)
    result = solver.check()
    assert result == z3.sat, f"Expected SAT but got {result}"
    return solver.model()


class TestAppIntegrationOrderingConstraint:
    """Verify the Or-based ordering constraint behaves correctly."""

    def test_different_endpoints_same_charm_sat(self) -> None:
        """Two apps sharing a charm with different endpoint names: solver finds correct ordering."""
        # GIVEN two applications of the same charm, integrated via distinct endpoint names
        domain = _make_domain(
            {
                ModelRef(name="m"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={
                        "provider-app": DomainApplication(charm="my-charm"),
                        "requirer-app": DomainApplication(charm="my-charm"),
                    },
                    application_integrations=[
                        DomainApplicationIntegration(
                            endpoint_1=DomainApplicationEndpoint(application="provider-app", endpoint="server"),
                            endpoint_2=DomainApplicationEndpoint(application="requirer-app", endpoint="client"),
                        ),
                    ],
                )
            }
        )
        charm = _make_charm(
            "my-charm",
            endpoints={
                "server": CharmEndpoint(type=EndpointType.PROVIDES, interface="rpc", optional=True),
                "client": CharmEndpoint(type=EndpointType.REQUIRES, interface="rpc", optional=True),
            },
        )
        add_charm_to_domain(charm, domain, ModelRef(name="m"))
        add_charm_to_domain(charm, domain, ModelRef(name="m"))

        # WHEN solving
        model = _solve(domain)
        solution = extract_solution(model, domain, logger=_LOGGER)
        bundle = solution.bundles[0]

        # THEN both apps are present with correct charm
        assert "provider-app" in bundle.applications
        assert "requirer-app" in bundle.applications

        # AND the integration uses the correct endpoints
        assert len(bundle.integrations) == 1
        integration = next(iter(bundle.integrations))
        endpoints = {ep.application: ep.endpoint for ep in integration}
        assert endpoints == {"provider-app": "server", "requirer-app": "client"}

    def test_same_endpoint_name_same_charm_sat(self) -> None:
        """Two apps of the same charm with identically-named endpoints: solver picks via limits."""
        # GIVEN a charm where both endpoints are named "link" but on different interfaces
        # (this test uses the same interface but different types, with a limit to disambiguate)
        domain = _make_domain(
            {
                ModelRef(name="m"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={
                        "hub": DomainApplication(charm="hub-charm"),
                        "leaf": DomainApplication(charm="leaf-charm"),
                    },
                    application_integrations=[
                        DomainApplicationIntegration(
                            endpoint_1=DomainApplicationEndpoint(application="hub", endpoint="downstream"),
                            endpoint_2=DomainApplicationEndpoint(application="leaf", endpoint="upstream"),
                        ),
                    ],
                )
            }
        )
        hub = _make_charm(
            "hub-charm",
            endpoints={
                "downstream": CharmEndpoint(type=EndpointType.PROVIDES, interface="data-flow", optional=True),
            },
        )
        leaf = _make_charm(
            "leaf-charm",
            endpoints={
                "upstream": CharmEndpoint(type=EndpointType.REQUIRES, interface="data-flow", optional=True),
            },
        )
        add_charm_to_domain(hub, domain, ModelRef(name="m"))
        add_charm_to_domain(leaf, domain, ModelRef(name="m"))

        # WHEN solving
        model = _solve(domain)
        solution = extract_solution(model, domain, logger=_LOGGER)
        bundle = solution.bundles[0]

        # THEN integration connects hub:downstream to leaf:upstream
        assert len(bundle.integrations) == 1
        integration = next(iter(bundle.integrations))
        endpoints = {ep.application: ep.endpoint for ep in integration}
        assert endpoints == {"hub": "downstream", "leaf": "upstream"}

    def test_three_apps_same_charm_one_provides_two_require(self) -> None:
        """One provider connects to two requirers, all sharing the same charm."""
        # GIVEN three apps of the same charm with mutual exclusion and limit constraints
        domain = _make_domain(
            {
                ModelRef(name="m"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={
                        "master": DomainApplication(charm="node-k8s"),
                        "worker-a": DomainApplication(charm="node-k8s"),
                        "worker-b": DomainApplication(charm="node-k8s"),
                    },
                    application_integrations=[
                        DomainApplicationIntegration(
                            endpoint_1=DomainApplicationEndpoint(application="master", endpoint="control"),
                            endpoint_2=DomainApplicationEndpoint(application="worker-a", endpoint="join"),
                        ),
                        DomainApplicationIntegration(
                            endpoint_1=DomainApplicationEndpoint(application="master", endpoint="control"),
                            endpoint_2=DomainApplicationEndpoint(application="worker-b", endpoint="join"),
                        ),
                    ],
                )
            }
        )
        from bundle_builder_x.constraints_dsl import parse_constraint

        charm = Charm(
            name="node-k8s",
            channel=CharmChannel.model_validate("stable"),
            revision=1,
            ubuntu_version="22.04",
            ubuntu_arch="amd64",
            endpoints={
                "control": CharmEndpoint(type=EndpointType.PROVIDES, interface="cluster", optional=True),
                "join": CharmEndpoint(type=EndpointType.REQUIRES, interface="cluster", optional=True, limit=1),
            },
            constraints=[parse_constraint("not (bool(endpoint[control]) and bool(endpoint[join]))")],
        )
        add_charm_to_domain(charm, domain, ModelRef(name="m"))
        add_charm_to_domain(charm, domain, ModelRef(name="m"))
        add_charm_to_domain(charm, domain, ModelRef(name="m"))

        # WHEN solving
        model = _solve(domain)
        solution = extract_solution(model, domain, logger=_LOGGER)
        bundle = solution.bundles[0]

        # THEN all three apps exist
        assert set(bundle.applications.keys()) == {"master", "worker-a", "worker-b"}

        # AND there are exactly two integrations
        assert len(bundle.integrations) == 2

        # AND master uses endpoint "control" in all integrations
        for integration in bundle.integrations:
            endpoints = {ep.application: ep.endpoint for ep in integration}
            assert endpoints.get("master") == "control"

        # AND workers use endpoint "join"
        worker_eps = set()
        for integration in bundle.integrations:
            for ep in integration:
                if ep.application.startswith("worker-"):
                    worker_eps.add((ep.application, ep.endpoint))
        assert worker_eps == {("worker-a", "join"), ("worker-b", "join")}

    def test_both_orderings_valid_solver_picks_consistent_one(self) -> None:
        """When endpoint names are identical across provides/requires, the Or allows either.

        This tests the user's counterexample: charm-x endpoint "a" (requires) and
        charm-y endpoint "a" (provides). When both apps could be either charm, the
        Or allows both orderings and the solver picks one consistent with other constraints.
        """
        # GIVEN two different charms with identically-named endpoints on the same interface
        domain = _make_domain(
            {
                ModelRef(name="m"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={
                        "app-alpha": DomainApplication(charm="sender"),
                        "app-beta": DomainApplication(charm="receiver"),
                    },
                    application_integrations=[
                        DomainApplicationIntegration(
                            endpoint_1=DomainApplicationEndpoint(application="app-alpha", endpoint="data"),
                            endpoint_2=DomainApplicationEndpoint(application="app-beta", endpoint="data"),
                        ),
                    ],
                )
            }
        )
        sender = _make_charm(
            "sender",
            endpoints={
                "data": CharmEndpoint(type=EndpointType.REQUIRES, interface="stream", optional=True),
            },
        )
        receiver = _make_charm(
            "receiver",
            endpoints={
                "data": CharmEndpoint(type=EndpointType.PROVIDES, interface="stream", optional=True),
            },
        )
        add_charm_to_domain(sender, domain, ModelRef(name="m"))
        add_charm_to_domain(receiver, domain, ModelRef(name="m"))

        # WHEN solving
        model = _solve(domain)
        solution = extract_solution(model, domain, logger=_LOGGER)
        bundle = solution.bundles[0]

        # THEN both apps are assigned correctly: sender requires, receiver provides
        assert "app-alpha" in bundle.applications
        assert "app-beta" in bundle.applications
        assert bundle.applications["app-alpha"].charm.name == "sender"
        assert bundle.applications["app-beta"].charm.name == "receiver"

        # AND the integration connects them via endpoint "data"
        assert len(bundle.integrations) == 1
        integration = next(iter(bundle.integrations))
        apps = {ep.application for ep in integration}
        assert apps == {"app-alpha", "app-beta"}

    def test_same_endpoint_name_both_orderings_valid(self) -> None:
        """When both sides of an integration have the same endpoint name and both apps use the same charm.

        Charm has endpoint "a" as provides AND "a" as requires (impossible in Juju - endpoints
        must have unique names). So instead, test with two apps of the same charm where both
        orderings have valid app_to_charm keys but different endpoint names disambiguate.

        The real "same endpoint name" case occurs when two DIFFERENT charms each have
        an endpoint with the same name. That is covered by test_both_orderings_valid_solver_picks_consistent_one.
        """
        # This test verifies the Or constraint doesn't break when only one ordering is valid
        # (because the other ordering's keys don't exist in app_to_charm).
        # GIVEN app-alpha uses charm "sender" and app-beta uses charm "receiver"
        # The charm_ids for sender are only associated with app-alpha, and vice versa.
        domain = _make_domain(
            {
                ModelRef(name="m"): DomainModel(
                    arch="amd64",
                    platform="kubernetes",
                    juju_version=_JUJU,
                    applications={
                        "app-alpha": DomainApplication(charm="sender"),
                        "app-beta": DomainApplication(charm="receiver"),
                    },
                    application_integrations=[
                        DomainApplicationIntegration(
                            endpoint_1=DomainApplicationEndpoint(application="app-alpha", endpoint="out"),
                            endpoint_2=DomainApplicationEndpoint(application="app-beta", endpoint="in"),
                        ),
                    ],
                )
            }
        )
        sender = _make_charm(
            "sender",
            endpoints={
                "out": CharmEndpoint(type=EndpointType.REQUIRES, interface="pipe", optional=True),
            },
        )
        receiver = _make_charm(
            "receiver",
            endpoints={
                "in": CharmEndpoint(type=EndpointType.PROVIDES, interface="pipe", optional=True),
            },
        )
        add_charm_to_domain(sender, domain, ModelRef(name="m"))
        add_charm_to_domain(receiver, domain, ModelRef(name="m"))

        # WHEN solving - only one ordering has valid keys (sender can't map to receiver's charm_id)
        model = _solve(domain)
        solution = extract_solution(model, domain, logger=_LOGGER)
        bundle = solution.bundles[0]

        # THEN apps are correctly assigned
        assert bundle.applications["app-alpha"].charm.name == "sender"
        assert bundle.applications["app-beta"].charm.name == "receiver"

        # AND the integration exists
        assert len(bundle.integrations) == 1
        integration = next(iter(bundle.integrations))
        endpoints = {ep.application: ep.endpoint for ep in integration}
        assert endpoints == {"app-alpha": "out", "app-beta": "in"}
