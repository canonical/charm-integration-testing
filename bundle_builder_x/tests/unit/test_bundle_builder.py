# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for bundle_builder.py."""

from itertools import repeat
from typing import Iterator

import z3  # type: ignore[import-untyped]

from bundle_builder_x.assertion_tags import CharmPayload, PeerChannelMismatchTag, SubordinateBaseMismatchTag
from bundle_builder_x.bundle_builder import BundleBuilder
from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointScope, EndpointType
from bundle_builder_x.charmhub import CharmhubClient
from bundle_builder_x.charmhub_http import CharmReleaseNotFoundException
from bundle_builder_x.constraints import add_constraints
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


class _FakeCharmhubClient(CharmhubClient):
    """Minimal typed stub for CharmhubClient, used in BundleBuilder unit tests."""

    def __init__(
        self,
        charm_responses: list[Charm | Exception] | Charm | Exception | None = None,
        find_result: set[str] | None = None,
    ) -> None:
        # Bypass CharmhubClient.__init__ - no HTTP client needed for unit tests.
        if charm_responses is None:
            self._responses: Iterator[Charm | Exception] = iter([])
        elif isinstance(charm_responses, list):
            self._responses = iter(charm_responses)
        else:
            # Single Charm or Exception: repeat indefinitely.
            self._responses = repeat(charm_responses)
        self._find_result: set[str] = find_result if find_result is not None else set()
        self.charm_from_store_calls: list[dict[str, object]] = []

    def charm_from_store(
        self,
        charm_name: str,
        ubuntu_arch: str,
        juju_version: JujuVersion | None = None,
        platform: str | None = None,
        charm_track: str | None = None,
        charm_risk: str | None = None,
        charm_revision: int | None = None,
        ubuntu_version: str | None = None,
    ) -> Charm:
        self.charm_from_store_calls.append(
            {
                "charm_name": charm_name,
                "ubuntu_arch": ubuntu_arch,
                "juju_version": juju_version,
                "platform": platform,
                "charm_track": charm_track,
                "charm_risk": charm_risk,
                "charm_revision": charm_revision,
                "ubuntu_version": ubuntu_version,
            }
        )
        resp = next(self._responses)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def find_charms(
        self,
        provides: str | None = None,
        requires: str | None = None,
        platform: str | None = None,
    ) -> set[str]:
        return self._find_result


def _make_charm(name: str, endpoints: dict[str, CharmEndpoint], ubuntu_version: str = "22.04") -> Charm:
    return Charm(
        name=name,
        channel=_CHANNEL,
        revision=1,
        ubuntu_version=ubuntu_version,
        ubuntu_arch="amd64",
        endpoints=endpoints,
    )


def _domain_with_base_mismatch() -> Domain:
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
            {"juju-info": CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", scope=EndpointScope.GLOBAL)},
            ubuntu_version="22.04",
        ),
        domain,
        ModelRef(name="m"),
    )
    add_charm_to_domain(
        _make_charm(
            "nrpe",
            {
                "general-info": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="juju-info", scope=EndpointScope.CONTAINER
                )
            },
            ubuntu_version="24.04",
        ),
        domain,
        ModelRef(name="m"),
    )
    return domain


def _mismatch_tag() -> SubordinateBaseMismatchTag:
    return SubordinateBaseMismatchTag(
        subordinate_charm_name="nrpe",
        subordinate_charm_id=1,
        subordinate_endpoint="general-info",
        principal_charm_name="ubuntu",
        principal_charm_id=0,
        principal_endpoint="juju-info",
        subordinate_base="24.04",
        principal_base="22.04",
    )


class TestHandleSubordinateBaseMismatch:
    """BundleBuilder._handle_subordinate_base_mismatch."""

    def test_returns_true_and_expands_when_subordinate_variant_found(self) -> None:
        domain = _domain_with_base_mismatch()
        nrpe_2204 = _make_charm(
            "nrpe",
            {
                "general-info": CharmEndpoint(
                    type=EndpointType.REQUIRES, interface="juju-info", scope=EndpointScope.CONTAINER
                )
            },
            ubuntu_version="22.04",
        )
        fake = _FakeCharmhubClient(
            charm_responses=[
                nrpe_2204,
                CharmReleaseNotFoundException("ubuntu", "No release on 24.04"),
            ]
        )
        builder = BundleBuilder(charmhub_client=fake)

        result = builder._handle_subordinate_base_mismatch(_mismatch_tag(), domain)

        assert result is True
        assert len(domain.charms) == 3
        assert domain.charms[2].spec.name == "nrpe"
        assert domain.charms[2].spec.ubuntu_version == "22.04"

    def test_returns_true_and_expands_when_principal_variant_found(self) -> None:
        domain = _domain_with_base_mismatch()
        ubuntu_2404 = _make_charm(
            "ubuntu",
            {"juju-info": CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", scope=EndpointScope.GLOBAL)},
            ubuntu_version="24.04",
        )
        fake = _FakeCharmhubClient(
            charm_responses=[
                CharmReleaseNotFoundException("nrpe", "No release on 22.04"),
                ubuntu_2404,
            ]
        )
        builder = BundleBuilder(charmhub_client=fake)

        result = builder._handle_subordinate_base_mismatch(_mismatch_tag(), domain)

        assert result is True
        assert len(domain.charms) == 3
        assert domain.charms[2].spec.name == "ubuntu"
        assert domain.charms[2].spec.ubuntu_version == "24.04"

    def test_returns_false_when_no_variant_found(self) -> None:
        domain = _domain_with_base_mismatch()
        fake = _FakeCharmhubClient(charm_responses=CharmReleaseNotFoundException("nrpe", "No release"))
        builder = BundleBuilder(charmhub_client=fake)

        result = builder._handle_subordinate_base_mismatch(_mismatch_tag(), domain)

        assert result is False
        assert len(domain.charms) == 2


class TestGetCharmsForEndpoint:
    """BundleBuilder._get_charms_for_endpoint: ubuntu_version forwarding for container-scoped endpoints."""

    def _domain_with_subordinate(self, ubuntu_version: str = "22.04") -> Domain:
        """Domain containing only a subordinate charm (requires juju-info, scope=container)."""
        domain = Domain()
        domain.models[ModelRef(name="m")] = DomainModel(
            arch="amd64",
            platform="machine",
            juju_version=_JUJU,
            applications={"nrpe": DomainApplication(charm="nrpe")},
        )
        add_charm_to_domain(
            _make_charm(
                "nrpe",
                {
                    "general-info": CharmEndpoint(
                        type=EndpointType.REQUIRES, interface="juju-info", scope=EndpointScope.CONTAINER
                    )
                },
                ubuntu_version=ubuntu_version,
            ),
            domain,
            ModelRef(name="m"),
        )
        return domain

    def _domain_with_global_endpoint(self) -> tuple[Domain, int]:
        """Domain containing a charm with a global-scope requires endpoint."""
        domain = Domain()
        domain.models[ModelRef(name="m")] = DomainModel(
            arch="amd64",
            platform="machine",
            juju_version=_JUJU,
            applications={"app": DomainApplication(charm="app")},
        )
        charm_id = add_charm_to_domain(
            _make_charm(
                "app", {"db": CharmEndpoint(type=EndpointType.REQUIRES, interface="pgsql", scope=EndpointScope.GLOBAL)}
            ),
            domain,
            ModelRef(name="m"),
        )
        return domain, charm_id

    def test_container_scope_passes_ubuntu_version_to_charm_from_store(self) -> None:
        # GIVEN a subordinate charm on 22.04 with a container-scoped requires endpoint
        domain = self._domain_with_subordinate(ubuntu_version="22.04")
        ubuntu = _make_charm(
            "ubuntu",
            {"juju-info": CharmEndpoint(type=EndpointType.PROVIDES, interface="juju-info", scope=EndpointScope.GLOBAL)},
        )
        fake = _FakeCharmhubClient(charm_responses=ubuntu, find_result={"ubuntu"})
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN fetching charms to fulfill the subordinate's container-scoped endpoint
        results = builder._get_charms_for_endpoint(0, "general-info", domain, ModelRef(name="m"))

        # THEN charm_from_store is called with ubuntu_version matching the subordinate's base
        assert results == [ubuntu]
        assert fake.charm_from_store_calls[0]["ubuntu_version"] == "22.04"

    def test_non_container_scope_passes_no_ubuntu_version(self) -> None:
        # GIVEN a charm with a global-scoped requires endpoint
        domain, charm_id = self._domain_with_global_endpoint()
        db = _make_charm("database", {"db": CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql")})
        fake = _FakeCharmhubClient(charm_responses=db, find_result={"database"})
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN fetching charms for the global-scoped endpoint
        builder._get_charms_for_endpoint(charm_id, "db", domain, ModelRef(name="m"))

        # THEN charm_from_store is called with ubuntu_version=None (base irrelevant for global scope)
        assert fake.charm_from_store_calls[0]["ubuntu_version"] is None

    def test_container_scope_skips_charm_when_base_not_available(self) -> None:
        # GIVEN no principal charm exists at the subordinate's base
        domain = self._domain_with_subordinate(ubuntu_version="22.04")
        fake = _FakeCharmhubClient(
            charm_responses=CharmReleaseNotFoundException("ubuntu", "no 22.04 release"),
            find_result={"ubuntu"},
        )
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN fetching charms for the container-scoped endpoint
        results = builder._get_charms_for_endpoint(0, "general-info", domain, ModelRef(name="m"))

        # THEN the charm is skipped and the result is empty
        assert results == []

    def test_container_scope_returns_empty_on_non_machine_platform(self) -> None:
        # GIVEN a subordinate charm on a kubernetes model
        domain = Domain()
        domain.models[ModelRef(name="k8s")] = DomainModel(
            arch="amd64",
            platform="kubernetes",
            juju_version=_JUJU,
            applications={"nrpe": DomainApplication(charm="nrpe")},
        )
        add_charm_to_domain(
            _make_charm(
                "nrpe",
                {
                    "general-info": CharmEndpoint(
                        type=EndpointType.REQUIRES, interface="juju-info", scope=EndpointScope.CONTAINER
                    )
                },
            ),
            domain,
            ModelRef(name="k8s"),
        )
        fake = _FakeCharmhubClient(find_result={"ubuntu"})
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN fetching charms for the container-scoped endpoint on a non-machine model
        results = builder._get_charms_for_endpoint(0, "general-info", domain, ModelRef(name="k8s"))

        # THEN no results are returned and no Charmhub queries were made
        assert results == []
        assert fake.charm_from_store_calls == []


def _make_charm_variant(revision: int, priority: float) -> Charm:
    return Charm(
        name="myapp",
        channel=_CHANNEL,
        revision=revision,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints={},
        priority=priority,
    )


def _domain_with_two_alternatives(
    priority_a: float = 1.0,
    priority_b: float = 2.0,
) -> tuple[Domain, int, int]:
    domain = Domain()
    model_ref = ModelRef(name="m")
    domain.models[model_ref] = DomainModel(
        arch="amd64",
        platform="kubernetes",
        juju_version=_JUJU,
        applications={"myapp": DomainApplication(charm="myapp")},
    )
    id_a = add_charm_to_domain(_make_charm_variant(revision=1, priority=priority_a), domain, model_ref)
    id_b = add_charm_to_domain(_make_charm_variant(revision=2, priority=priority_b), domain, model_ref)
    return domain, id_a, id_b


class TestOptimizeSolution:
    """BundleBuilder._optimize_solution."""

    def test_extra_constraints_are_applied_as_hard_constraints(self) -> None:
        # GIVEN a domain with two alternatives and an extra constraint forcing charm A out
        domain, id_a, id_b = _domain_with_two_alternatives()
        builder = BundleBuilder(charmhub_client=_FakeCharmhubClient())
        extra = [z3.Not(domain.charms[id_a].exists)]

        # WHEN optimize is called with the extra constraint
        model = builder._optimize_solution(domain, extra_constraints=extra)

        # THEN charm A is absent from the solution
        assert not z3.is_true(model.eval(domain.charms[id_a].exists, model_completion=True))
        # AND charm B covers the application
        assert z3.is_true(model.eval(domain.charms[id_b].exists, model_completion=True))

    def test_selects_higher_priority_charm(self) -> None:
        # GIVEN charm B has higher priority (lower optimizer cost) than charm A
        domain, id_a, id_b = _domain_with_two_alternatives(priority_a=1.0, priority_b=2.0)
        builder = BundleBuilder(charmhub_client=_FakeCharmhubClient())

        # WHEN optimize is called without extra constraints
        model = builder._optimize_solution(domain)

        # THEN the optimizer selects charm B (lower cost = preferred)
        assert z3.is_true(model.eval(domain.charms[id_b].exists, model_completion=True))
        assert not z3.is_true(model.eval(domain.charms[id_a].exists, model_completion=True))


class TestSolve:
    """BundleBuilder._solve."""

    def test_returns_valid_solution(self) -> None:
        # GIVEN a domain with two charm alternatives
        domain, id_a, id_b = _domain_with_two_alternatives()
        builder = BundleBuilder(charmhub_client=_FakeCharmhubClient())

        # WHEN _solve runs
        model = builder._solve(domain)

        # THEN the application is covered by exactly one charm variant
        app = domain.models[ModelRef(name="m")].applications["myapp"]
        mapped_count = sum(1 for v in app.charm_ids.values() if z3.is_true(model.eval(v, model_completion=True)))
        assert mapped_count == 1


class TestIterativeDescent:
    """BundleBuilder._iterative_descent."""

    def test_selects_higher_priority_charm(self) -> None:
        # GIVEN a domain where charm B has higher priority
        domain, id_a, id_b = _domain_with_two_alternatives(priority_a=1.0, priority_b=2.0)
        builder = BundleBuilder(charmhub_client=_FakeCharmhubClient())
        charm_cost_expr, integration_cost_expr, num_units_cost_expr = BundleBuilder._build_cost_exprs(domain)

        # WHEN _iterative_descent runs directly (bypassing z3.Optimize)
        model = builder._iterative_descent(
            domain,
            charm_cost_expr,
            integration_cost_expr,
            num_units_cost_expr,
            initial_model=None,
            extra_constraints=None,
        )

        # THEN it selects charm B (higher priority)
        assert z3.is_true(model.eval(domain.charms[id_b].exists, model_completion=True))
        assert not z3.is_true(model.eval(domain.charms[id_a].exists, model_completion=True))

    def test_initial_model_is_used_as_seed(self) -> None:
        # GIVEN a domain and an initial SAT model already pinning charm A
        domain, id_a, id_b = _domain_with_two_alternatives(priority_a=1.0, priority_b=2.0)
        builder = BundleBuilder(charmhub_client=_FakeCharmhubClient())
        charm_cost_expr, integration_cost_expr, num_units_cost_expr = BundleBuilder._build_cost_exprs(domain)

        # Obtain an initial model from a plain solver
        solver = z3.Solver()
        add_constraints(solver, domain)
        assert solver.check() == z3.sat
        initial = solver.model()

        # WHEN _iterative_descent is seeded with that model
        model = builder._iterative_descent(
            domain,
            charm_cost_expr,
            integration_cost_expr,
            num_units_cost_expr,
            initial_model=initial,
            extra_constraints=None,
        )

        # THEN it still converges to the optimal (charm B) from the seed
        assert z3.is_true(model.eval(domain.charms[id_b].exists, model_completion=True))


def _mismatch(
    anchor_id: int, peer_id: int, track: str | None = None, risk: str | None = None
) -> PeerChannelMismatchTag:
    return PeerChannelMismatchTag(
        charm=CharmPayload(charm_name="anchor", charm_id=anchor_id),
        endpoint="ep",
        peer_charm_name="peer",
        peer_charm_id=peer_id,
        required_track=track,
        required_risk=risk,
    )


class TestMergeMismatchTags:
    """BundleBuilder._merge_mismatch_tags."""

    def test_non_mismatch_tags_pass_through(self) -> None:
        # GIVEN a list containing only a non-mismatch tag
        tag = _mismatch_tag()

        # WHEN merged
        result = BundleBuilder._merge_mismatch_tags([tag])

        # THEN it is returned unchanged
        assert result == [tag]

    def test_single_mismatch_tag_passes_through(self) -> None:
        # GIVEN a single PeerChannelMismatchTag
        tag = _mismatch(anchor_id=0, peer_id=1, track="zed")

        # WHEN merged
        result = BundleBuilder._merge_mismatch_tags([tag])

        # THEN it is returned as-is
        assert result == [tag]

    def test_same_pair_track_and_risk_combined(self) -> None:
        # GIVEN two mismatch tags for the same (anchor, peer) pair - one carries track, one carries risk
        track_tag = _mismatch(anchor_id=0, peer_id=1, track="zed")
        risk_tag = _mismatch(anchor_id=0, peer_id=1, risk="edge")

        # WHEN merged
        result = BundleBuilder._merge_mismatch_tags([track_tag, risk_tag])

        # THEN they collapse to one tag with both fields set
        assert len(result) == 1
        merged = result[0]
        assert isinstance(merged, PeerChannelMismatchTag)
        assert merged.required_track == "zed"
        assert merged.required_risk == "edge"

    def test_different_pairs_kept_separate(self) -> None:
        # GIVEN mismatch tags for two different (anchor, peer) pairs
        tag_a = _mismatch(anchor_id=0, peer_id=1, track="zed")
        tag_b = _mismatch(anchor_id=0, peer_id=2, track="antelope")

        # WHEN merged
        result = BundleBuilder._merge_mismatch_tags([tag_a, tag_b])

        # THEN both are kept
        assert len(result) == 2

    def test_insertion_order_preserved(self) -> None:
        # GIVEN a mix: non-mismatch, mismatch pair A, non-mismatch, mismatch pair B
        non_mismatch = _mismatch_tag()
        tag_a = _mismatch(anchor_id=0, peer_id=1, track="zed")
        tag_a2 = _mismatch(anchor_id=0, peer_id=1, risk="edge")
        tag_b = _mismatch(anchor_id=0, peer_id=2, track="antelope")

        # WHEN merged
        result = BundleBuilder._merge_mismatch_tags([non_mismatch, tag_a, non_mismatch, tag_b, tag_a2])

        # THEN non-mismatch tags appear in their original positions and pair A is merged
        assert result[0] is non_mismatch
        assert result[1] is not tag_a  # pair A was replaced with a merged copy
        assert isinstance(result[1], PeerChannelMismatchTag)
        assert result[1].required_track == "zed"
        assert result[1].required_risk == "edge"
        assert result[2] is non_mismatch
        assert isinstance(result[3], PeerChannelMismatchTag)
        assert result[3].required_track == "antelope"
