# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for constraints.py."""

import z3  # type: ignore[import-untyped]

from bundle_builder_x.assertion_tags import AssertionTag, SubordinateBaseMismatchTag
from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointType
from bundle_builder_x.constraints import add_subordinate_constraints
from bundle_builder_x.domain import (
    Domain,
    DomainApplication,
    DomainModel,
    ModelRef,
    add_charm_to_domain,
)
from bundle_builder_x.juju_version import JujuVersion

_JUJU = JujuVersion(major=3, minor=6, patch=0)
_CHANNEL = CharmChannel(track="latest", risk="stable", branch="")


def _make_charm(name: str, endpoints: dict[str, CharmEndpoint], ubuntu_version: str = "22.04") -> Charm:
    return Charm(
        name=name,
        channel=_CHANNEL,
        revision=1,
        ubuntu_version=ubuntu_version,
        ubuntu_arch="amd64",
        endpoints=endpoints,
        platforms=["machine", "kubernetes"],
    )


def _machine_domain_with_pair(
    sub_version: str,
    principal_version: str,
    scope: str = "container",
) -> Domain:
    domain = Domain()
    domain.models[ModelRef(name="m")] = DomainModel(
        arch="amd64",
        platform="machine",
        juju_version=_JUJU,
        applications={
            "ubuntu": DomainApplication(charm="ubuntu"),
            "nrpe": DomainApplication(charm="nrpe"),
        },
    )
    add_charm_to_domain(
        _make_charm(
            "ubuntu",
            {"juju-info": CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", scope="global")},
            ubuntu_version=principal_version,
        ),
        domain,
        ModelRef(name="m"),
    )
    add_charm_to_domain(
        _make_charm(
            "nrpe",
            {"general-info": CharmEndpoint(type=EndpointType.REQUIRES, interface="juju-info", scope=scope)},
            ubuntu_version=sub_version,
        ),
        domain,
        ModelRef(name="m"),
    )
    return domain


class TestAddSubordinateConstraints:
    """add_subordinate_constraints: verifies what Z3 assertions are added to the solver."""

    def test_same_base_adds_no_assertions(self) -> None:
        domain = _machine_domain_with_pair(sub_version="22.04", principal_version="22.04")
        solver = z3.Solver()
        add_subordinate_constraints(solver, domain)
        assert len(solver.assertions()) == 0

    def test_different_base_adds_one_assertion(self) -> None:
        domain = _machine_domain_with_pair(sub_version="24.04", principal_version="22.04")
        solver = z3.Solver()
        solver.set("unsat_core", True)
        add_subordinate_constraints(solver, domain)
        assert len(solver.assertions()) == 1

    def test_different_base_assertion_tracked_with_mismatch_tag(self) -> None:
        domain = _machine_domain_with_pair(sub_version="24.04", principal_version="22.04")
        solver = z3.Solver()
        solver.set("unsat_core", True)
        add_subordinate_constraints(solver, domain)

        # assert_and_track stores Implies(tracking_bool, Not(integration.exists)).
        # The antecedent (arg 0) is the Z3 bool constant whose name is the encoded tag.
        assert len(solver.assertions()) == 1
        decoded = AssertionTag.decode(str(solver.assertions()[0].arg(0)))
        assert isinstance(decoded, SubordinateBaseMismatchTag)
        assert decoded.subordinate_base == "24.04"
        assert decoded.principal_base == "22.04"
        assert decoded.subordinate_charm_name == "nrpe"
        assert decoded.subordinate_endpoint == "general-info"
        assert decoded.principal_charm_name == "ubuntu"
        assert decoded.principal_endpoint == "juju-info"

    def test_global_scope_adds_no_assertions(self) -> None:
        domain = _machine_domain_with_pair(sub_version="22.04", principal_version="24.04", scope="global")
        solver = z3.Solver()
        add_subordinate_constraints(solver, domain)
        assert len(solver.assertions()) == 0

    def test_none_scope_adds_no_assertions(self) -> None:
        domain = Domain()
        domain.models[ModelRef(name="m")] = DomainModel(
            arch="amd64",
            platform="machine",
            juju_version=_JUJU,
            applications={
                "app": DomainApplication(charm="app"),
                "svc": DomainApplication(charm="svc"),
            },
        )
        add_charm_to_domain(
            _make_charm("app", {"data": CharmEndpoint(type=EndpointType.REQUIRES, interface="data")}),
            domain,
            ModelRef(name="m"),
        )
        add_charm_to_domain(
            _make_charm("svc", {"data": CharmEndpoint(type=EndpointType.PROVIDES, interface="data")}),
            domain,
            ModelRef(name="m"),
        )
        solver = z3.Solver()
        add_subordinate_constraints(solver, domain)
        assert len(solver.assertions()) == 0
