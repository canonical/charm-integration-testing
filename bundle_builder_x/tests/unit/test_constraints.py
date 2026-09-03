# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for constraints.py."""

import z3  # type: ignore[import-untyped]

from bundle_builder_x.assertion_tags import AssertionTag, SubordinateBaseMismatchTag
from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointType
from bundle_builder_x.constraints import add_charm_constraints, add_subordinate_constraints
from bundle_builder_x.domain import (
    Domain,
    DomainApplication,
    DomainCharmIntegration,
    DomainModel,
    ModelRef,
    add_charm_to_domain,
    pair_charms_in_domain,
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
    ubuntu_id = add_charm_to_domain(
        _make_charm(
            "ubuntu",
            {"juju-info": CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", scope="global")},
            ubuntu_version=principal_version,
        ),
        domain,
        ModelRef(name="m"),
    )
    nrpe_id = add_charm_to_domain(
        _make_charm(
            "nrpe",
            {"general-info": CharmEndpoint(type=EndpointType.REQUIRES, interface="juju-info", scope=scope)},
            ubuntu_version=sub_version,
        ),
        domain,
        ModelRef(name="m"),
    )
    pair_charms_in_domain(domain, ubuntu_id, nrpe_id)
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


def _domain_with_pair(interface: str = "data") -> tuple[Domain, int, int]:
    domain = Domain()
    domain.models[ModelRef(name="m")] = DomainModel(
        arch="amd64",
        platform="kubernetes",
        juju_version=_JUJU,
        applications={
            "app": DomainApplication(charm="app"),
            "svc": DomainApplication(charm="svc"),
        },
    )
    app_id = add_charm_to_domain(
        _make_charm("app", {"data": CharmEndpoint(type=EndpointType.REQUIRES, interface=interface)}),
        domain,
        ModelRef(name="m"),
    )
    svc_id = add_charm_to_domain(
        _make_charm("svc", {"data": CharmEndpoint(type=EndpointType.PROVIDES, interface=interface)}),
        domain,
        ModelRef(name="m"),
    )
    return domain, app_id, svc_id


class TestAddCharmConstraintsDuplicateIntegrations:
    """Regression test for the "named assertion defined twice" Z3 crash observed in CI
    (e.g. test_result 13011538 / test_execution 890021 against grafana-agent-k8s), where
    add_charm_constraints emitted the same CharmExistsFromIntegrationTag twice for two
    DomainCharmIntegration entries sharing an identical
    (requires_charm_id, requires_endpoint, provides_charm_id, provides_endpoint) key.
    pair_charms_in_domain() itself de-duplicates on this same key, so this test appends
    a duplicate entry directly to domain.charm_integrations to reproduce the crash
    mechanism at the add_charm_constraints call site regardless of how the duplicate
    entry came to exist upstream.
    """

    def test_duplicate_integration_entries_do_not_crash_the_solver(self) -> None:
        domain, app_id, svc_id = _domain_with_pair()
        pair_charms_in_domain(domain, app_id, svc_id)
        assert len(domain.charm_integrations) == 1

        # Manually append a duplicate entry with the exact same key, simulating the
        # scenario observed in production where a second, otherwise-identical
        # DomainCharmIntegration ends up in the domain.
        original = domain.charm_integrations[0]
        duplicate = DomainCharmIntegration(
            exists=z3.Bool("duplicate_exists"),
            requires_charm_id=original.requires_charm_id,
            requires_endpoint=original.requires_endpoint,
            provides_charm_id=original.provides_charm_id,
            provides_endpoint=original.provides_endpoint,
        )
        domain.charm_integrations.append(duplicate)
        assert len(domain.charm_integrations) == 2

        solver = z3.Solver()
        solver.set("unsat_core", True)
        # Before the fix, this raised z3.z3types.Z3Exception: b'named assertion defined twice'.
        add_charm_constraints(solver, domain)

        # Only one assertion should be tracked per unique tag, even though two
        # DomainCharmIntegration entries produced the same tag.
        tags = [AssertionTag.decode(str(a.arg(0))) for a in solver.assertions()]
        encoded_tags = {t.encode() for t in tags}
        assert len(encoded_tags) == len(tags), "expected no duplicate assertion tags"

        # Both DomainCharmIntegration.exists vars (the original and the duplicate) must
        # still imply the charm exists, not just the one that happened to win the dedup.
        # A naive dedup keyed only on the tag (dropping the duplicate's `exists` var
        # entirely) would leave this unconstrained and these checks would come back SAT.
        for exists_var in (original.exists, duplicate.exists):
            for charm_id in (app_id, svc_id):
                solver.push()
                solver.add(exists_var, z3.Not(domain.charms[charm_id].exists))
                assert solver.check() == z3.unsat, f"expected {exists_var} to imply charm {charm_id} exists"
                solver.pop()
