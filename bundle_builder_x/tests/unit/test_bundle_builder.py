# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for bundle_builder.py."""

from itertools import repeat
from typing import Iterator

import z3  # type: ignore[import-untyped]

from bundle_builder_x import CharmReleaseNotFoundException
from bundle_builder_x.assertion_tags import (
    AppEndpointPayload,
    ApplicationExistsTag,
    ApplicationIntegrationExistsTag,
    CharmEndpointNonOptionalTag,
    CharmEndpointPayload,
    CharmPayload,
    IntegrationFeatureMismatchTag,
    PeerChannelMismatchTag,
    SubordinateBaseMismatchTag,
)
from bundle_builder_x.bundle_builder import BundleBuilder, UncompletableBundleError
from bundle_builder_x.bundle_diagnostics import (
    ApplicationReleaseDiagnostic,
    BundleBuildFailureDiagnostic,
    BundleBuildFailureKind,
    DiagnosticEndpoint,
    FeatureMismatchDiagnostic,
    UnfulfilledEndpointDiagnostic,
    UnresolvedApplicationDiagnostic,
    UnresolvedIntegrationDiagnostic,
)
from bundle_builder_x.charm import Charm, CharmChannel, CharmEndpoint, EndpointScope, EndpointType
from bundle_builder_x.charmhub import CharmhubClient
from bundle_builder_x.constraints import add_constraints
from bundle_builder_x.domain import (
    Domain,
    DomainApplication,
    DomainModel,
    ModelRef,
    add_charm_to_domain,
    pair_charms_in_domain,
)
from bundle_builder_x.juju_version import JujuVersion
from bundle_builder_x.overrides import OverridesClient

_JUJU = JujuVersion(major=3, minor=6, patch=0)
_CHANNEL = CharmChannel(track="latest", risk="stable", branch="")


class _FakeOverridesClient(OverridesClient):
    """Minimal stub for OverridesClient used in unit tests.

    Returns a per-charm priority from `priorities` (defaulting to 1.0 for any
    charm not listed), so tests can exercise priority-based sorting.
    """

    def __init__(self, priorities: dict[str, float] | None = None) -> None:
        super().__init__()
        self._priorities = priorities or {}

    def get_charm_priority(self, charm: str) -> float:
        return self._priorities.get(charm, 1.0)


class _FakeCharmhubClient(CharmhubClient):
    """Minimal typed stub for CharmhubClient, used in BundleBuilder unit tests."""

    def __init__(
        self,
        charm_responses: list[Charm | Exception] | Charm | Exception | None = None,
        find_result: set[str] | None = None,
        priorities: dict[str, float] | None = None,
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
        self.find_charms_calls: list[dict[str, object]] = []
        self.overrides_client = _FakeOverridesClient(priorities)

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
        self.find_charms_calls.append({"provides": provides, "requires": requires, "platform": platform})
        return self._find_result


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
        assert any(
            {integration.requires_charm_id, integration.provides_charm_id} == {0, 2}
            for integration in domain.charm_integrations
        )

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
        assert any(
            {integration.requires_charm_id, integration.provides_charm_id} == {1, 2}
            for integration in domain.charm_integrations
        )

    def test_returns_false_when_no_variant_found(self) -> None:
        domain = _domain_with_base_mismatch()
        fake = _FakeCharmhubClient(charm_responses=CharmReleaseNotFoundException("nrpe", "No release"))
        builder = BundleBuilder(charmhub_client=fake)

        result = builder._handle_subordinate_base_mismatch(_mismatch_tag(), domain)

        assert result is False
        assert len(domain.charms) == 2


class TestGetCharmsForEndpoint:
    """BundleBuilder._get_charms_for_endpoint and _add_available_charms: candidate selection."""

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
        charms_before = len(domain.charms)

        # WHEN adding a candidate charm for the subordinate's container-scoped endpoint
        result = builder._add_available_charms(["ubuntu"], 0, "general-info", domain, ModelRef(name="m"))

        # THEN the charm is added successfully and the domain grows by one charm
        assert result is True
        assert len(domain.charms) == charms_before + 1

        # AND charm_from_store is called with ubuntu_version matching the subordinate's base
        assert fake.charm_from_store_calls[0]["ubuntu_version"] == "22.04"

    def test_non_container_scope_passes_no_ubuntu_version(self) -> None:
        # GIVEN a charm with a global-scoped requires endpoint
        domain, charm_id = self._domain_with_global_endpoint()
        db = _make_charm("database", {"db": CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql")})
        fake = _FakeCharmhubClient(charm_responses=db, find_result={"database"})
        builder = BundleBuilder(charmhub_client=fake)
        charms_before = len(domain.charms)

        # WHEN adding a candidate charm for the global-scoped endpoint
        result = builder._add_available_charms(["database"], charm_id, "db", domain, ModelRef(name="m"))

        # THEN the charm is added successfully and the domain grows by one charm
        assert result is True
        assert len(domain.charms) == charms_before + 1

        # AND charm_from_store is called with ubuntu_version=None (base irrelevant for global scope)
        assert fake.charm_from_store_calls[0]["ubuntu_version"] is None

    def test_adds_all_compatible_candidates(self) -> None:
        # GIVEN two compatible candidates for one unresolved endpoint
        domain, charm_id = self._domain_with_global_endpoint()
        candidates: list[Charm | Exception] = [
            _make_charm("database-a", {"db": CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql")}),
            _make_charm("database-b", {"db": CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql")}),
        ]
        builder = BundleBuilder(charmhub_client=_FakeCharmhubClient(charm_responses=candidates))

        # WHEN expanding the candidate list
        result = builder._add_available_charms(
            ["database-a", "database-b"],
            charm_id,
            "db",
            domain,
            ModelRef(name="m"),
        )

        # THEN both alternatives are available to the optimizer
        assert result is True
        assert [charm.spec.name for charm in domain.charms] == ["app", "database-a", "database-b"]

    def test_stops_after_first_candidate_for_feasibility(self) -> None:
        # GIVEN two compatible candidates for one unresolved endpoint
        domain, charm_id = self._domain_with_global_endpoint()
        candidates: list[Charm | Exception] = [
            _make_charm("database-a", {"db": CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql")}),
            _make_charm("database-b", {"db": CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql")}),
        ]
        fake = _FakeCharmhubClient(charm_responses=candidates)
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN expanding only enough to restore feasibility
        result = builder._add_available_charms(
            ["database-a", "database-b"],
            charm_id,
            "db",
            domain,
            ModelRef(name="m"),
            stop_after_first=True,
        )

        # THEN only the highest-priority viable candidate is materialized
        assert result is True
        assert [charm.spec.name for charm in domain.charms] == ["app", "database-a"]
        assert len(fake.charm_from_store_calls) == 1

    def test_container_scope_skips_charm_when_base_not_available(self) -> None:
        # GIVEN no principal charm exists at the subordinate's base
        domain = self._domain_with_subordinate(ubuntu_version="22.04")
        fake = _FakeCharmhubClient(
            charm_responses=CharmReleaseNotFoundException("ubuntu", "no 22.04 release"),
            find_result={"ubuntu"},
        )
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN adding a candidate charm for the container-scoped endpoint but it's unavailable
        result = builder._add_available_charms(["ubuntu"], 0, "general-info", domain, ModelRef(name="m"))

        # THEN no charm is added
        assert result is False

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

        # WHEN fetching candidate names for the container-scoped endpoint on a non-machine model
        results = builder._get_charms_for_endpoint(0, "general-info", domain, ModelRef(name="k8s"))

        # THEN no results are returned and no Charmhub queries were made
        assert results == []
        assert fake.charm_from_store_calls == []
        assert fake.find_charms_calls == []

    def test_candidates_sorted_by_priority_with_deterministic_tiebreak(self) -> None:
        # GIVEN a global-scoped endpoint with candidates of mixed priority, including a tie
        domain, charm_id = self._domain_with_global_endpoint()
        fake = _FakeCharmhubClient(
            find_result={"low", "high", "mid-b", "mid-a"},
            priorities={"low": 0.0, "high": 2.0, "mid-b": 1.0, "mid-a": 1.0},
        )
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN fetching candidate names for the endpoint
        results = builder._get_charms_for_endpoint(charm_id, "db", domain, ModelRef(name="m"))

        # THEN results are ordered by descending priority, with same-priority
        # candidates ("mid-a", "mid-b") broken deterministically by name ascending
        assert results == ["high", "mid-a", "mid-b", "low"]


def _make_charm_variant(revision: int, priority: float) -> Charm:
    return Charm(
        name="myapp",
        channel=_CHANNEL,
        revision=revision,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints={},
        priority=priority,
        platforms=["machine", "kubernetes"],
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


class TestExpandForEndpointContainerScope:
    """BundleBuilder._expand_for_endpoint: container-scoped endpoints never span models."""

    def _two_machine_model_domain_with_unresolved_subordinate(self) -> tuple[Domain, int]:
        """m1 has an unresolved subordinate; m2 has an unrelated charm providing juju-info."""
        domain = Domain()
        m1, m2 = ModelRef(name="m1"), ModelRef(name="m2")
        domain.models[m1] = DomainModel(
            arch="amd64",
            platform="machine",
            juju_version=_JUJU,
            applications={"nrpe": DomainApplication(charm="nrpe")},
        )
        domain.models[m2] = DomainModel(
            arch="amd64",
            platform="machine",
            juju_version=_JUJU,
            applications={"ubuntu": DomainApplication(charm="ubuntu")},
        )
        charm_id = add_charm_to_domain(
            _make_charm(
                "nrpe",
                {
                    "general-info": CharmEndpoint(
                        type=EndpointType.REQUIRES, interface="juju-info", scope=EndpointScope.CONTAINER
                    )
                },
            ),
            domain,
            m1,
        )
        add_charm_to_domain(
            _make_charm(
                "ubuntu",
                {
                    "juju-info": CharmEndpoint(
                        type=EndpointType.PROVIDES, interface="juju-info", scope=EndpointScope.GLOBAL
                    )
                },
            ),
            domain,
            m2,
        )
        return domain, charm_id

    def test_does_not_connect_existing_charm_in_another_model(self) -> None:
        # GIVEN a container-scoped endpoint unresolved in m1, and a compatible provider only in m2
        domain, charm_id = self._two_machine_model_domain_with_unresolved_subordinate()
        fake = _FakeCharmhubClient(find_result=set())
        builder = BundleBuilder(charmhub_client=fake)
        integrations_before = len(domain.charm_integrations)

        # WHEN expanding the endpoint
        result = builder._expand_for_endpoint(charm_id, "general-info", domain)

        # THEN nothing is connected across models (container scope can't span models)
        assert result is False
        assert len(domain.charm_integrations) == integrations_before

    def test_does_not_query_charmhub_for_other_models(self) -> None:
        # GIVEN the same domain, with Charmhub returning no candidates
        domain, charm_id = self._two_machine_model_domain_with_unresolved_subordinate()
        fake = _FakeCharmhubClient(find_result=set())
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN expanding the endpoint
        builder._expand_for_endpoint(charm_id, "general-info", domain)

        # THEN Charmhub is only queried once, for the owning model (m1) — never for m2
        assert len(fake.find_charms_calls) == 1


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


class TestAddOptimizationDuplicates:
    """BundleBuilder._add_optimization_duplicates."""

    def test_adds_one_duplicate_for_active_bidirectional_interface(self) -> None:
        # GIVEN an active charm that both requires and provides one interface
        domain = Domain()
        model_ref = ModelRef(name="m")
        domain.models[model_ref] = DomainModel(
            arch="amd64",
            platform="kubernetes",
            juju_version=_JUJU,
        )
        dual_id = add_charm_to_domain(
            _make_charm(
                "dual",
                {
                    "mesh-in": CharmEndpoint(type=EndpointType.REQUIRES, interface="mesh", optional=True),
                    "mesh-out": CharmEndpoint(type=EndpointType.PROVIDES, interface="mesh", optional=True),
                },
            ),
            domain,
            model_ref,
        )
        provider_id = add_charm_to_domain(
            _make_charm(
                "provider",
                {"mesh-out": CharmEndpoint(type=EndpointType.PROVIDES, interface="mesh", optional=True)},
            ),
            domain,
            model_ref,
        )
        pair_charms_in_domain(domain, dual_id, provider_id)
        integration = next(
            item
            for item in domain.charm_integrations
            if item.requires_charm_id == dual_id and item.provides_charm_id == provider_id
        )
        solver = z3.Solver()
        solver.add(domain.charms[dual_id].exists, domain.charms[provider_id].exists, integration.exists)
        assert solver.check() == z3.sat

        # WHEN preparing the satisfiable domain for optimization
        BundleBuilder._add_optimization_duplicates(domain, solver.model())

        # THEN one duplicate is exposed and paired with the active charms
        assert len(domain.charms) == 3
        assert domain.charms[2].spec == domain.charms[dual_id].spec
        assert any(
            {item.requires_charm_id, item.provides_charm_id} == {provider_id, 2} for item in domain.charm_integrations
        )

    def test_adds_provider_duplicate_for_parallel_required_endpoints(self) -> None:
        # GIVEN one active provider can satisfy two required endpoints on an active consumer
        domain = Domain()
        model_ref = ModelRef(name="m")
        domain.models[model_ref] = DomainModel(
            arch="amd64",
            platform="machine",
            juju_version=_JUJU,
        )
        consumer_id = add_charm_to_domain(
            _make_charm(
                "consumer",
                {
                    "database": CharmEndpoint(type=EndpointType.REQUIRES, interface="postgresql_client"),
                    "database-legacy": CharmEndpoint(type=EndpointType.REQUIRES, interface="pgsql"),
                },
            ),
            domain,
            model_ref,
        )
        provider_id = add_charm_to_domain(
            _make_charm(
                "database",
                {
                    "database": CharmEndpoint(
                        type=EndpointType.PROVIDES,
                        interface="postgresql_client",
                        optional=True,
                    ),
                    "db": CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql", optional=True),
                },
            ),
            domain,
            model_ref,
        )
        pair_charms_in_domain(domain, consumer_id, provider_id)
        integration = next(
            item
            for item in domain.charm_integrations
            if item.requires_charm_id == consumer_id and item.provides_charm_id == provider_id
        )
        solver = z3.Solver()
        solver.add(domain.charms[consumer_id].exists, domain.charms[provider_id].exists, integration.exists)
        assert solver.check() == z3.sat

        # WHEN preparing the satisfiable domain for optimization
        BundleBuilder._add_optimization_duplicates(domain, solver.model())

        # THEN only the provider is duplicated and paired with the active consumer
        assert len(domain.charms) == 3
        assert domain.charms[2].spec == domain.charms[provider_id].spec
        assert any(
            {item.requires_charm_id, item.provides_charm_id} == {consumer_id, 2} for item in domain.charm_integrations
        )


class TestPrepareOptimizationDomain:
    """BundleBuilder._prepare_optimization_domain."""

    def test_pairs_active_charms_to_expose_provider_sharing(self) -> None:
        # GIVEN two active consumers initially paired with separate compatible providers
        domain = Domain()
        model_ref = ModelRef(name="m")
        domain.models[model_ref] = DomainModel(
            arch="amd64",
            platform="machine",
            juju_version=_JUJU,
        )
        consumer_a = add_charm_to_domain(
            _make_charm("consumer-a", {"db": CharmEndpoint(type=EndpointType.REQUIRES, interface="db")}),
            domain,
            model_ref,
        )
        consumer_b = add_charm_to_domain(
            _make_charm("consumer-b", {"db": CharmEndpoint(type=EndpointType.REQUIRES, interface="db")}),
            domain,
            model_ref,
        )
        provider_a = add_charm_to_domain(
            _make_charm("provider-a", {"db": CharmEndpoint(type=EndpointType.PROVIDES, interface="db")}),
            domain,
            model_ref,
        )
        provider_b = add_charm_to_domain(
            _make_charm("provider-b", {"db": CharmEndpoint(type=EndpointType.PROVIDES, interface="db")}),
            domain,
            model_ref,
        )
        pair_charms_in_domain(domain, consumer_a, provider_a)
        pair_charms_in_domain(domain, consumer_b, provider_b)
        solver = z3.Solver()
        solver.add(*(charm.exists for charm in domain.charms))
        assert solver.check() == z3.sat

        # WHEN preparing the active graph for optimization
        BundleBuilder(charmhub_client=_FakeCharmhubClient())._prepare_optimization_domain(domain, solver.model())

        # THEN either active provider can serve either active consumer
        assert any(
            item.requires_charm_id == consumer_a and item.provides_charm_id == provider_b
            for item in domain.charm_integrations
        )
        assert any(
            item.requires_charm_id == consumer_b and item.provides_charm_id == provider_a
            for item in domain.charm_integrations
        )

    def test_pairs_inactive_replacement_with_active_graph(self) -> None:
        # GIVEN an inactive base replacement discovered for an active principal
        domain = Domain()
        model_ref = ModelRef(name="m")
        domain.models[model_ref] = DomainModel(
            arch="amd64",
            platform="machine",
            juju_version=_JUJU,
        )
        principal = _make_charm(
            "principal",
            {
                "cni": CharmEndpoint(
                    type=EndpointType.PROVIDES,
                    interface="cni",
                    scope=EndpointScope.CONTAINER,
                ),
                "control": CharmEndpoint(type=EndpointType.PROVIDES, interface="control"),
            },
            ubuntu_version="24.04",
        )
        principal_id = add_charm_to_domain(principal, domain, model_ref)
        subordinate_id = add_charm_to_domain(
            _make_charm(
                "subordinate",
                {
                    "cni": CharmEndpoint(
                        type=EndpointType.REQUIRES,
                        interface="cni",
                        scope=EndpointScope.CONTAINER,
                    )
                },
                ubuntu_version="22.04",
            ),
            domain,
            model_ref,
        )
        consumer_id = add_charm_to_domain(
            _make_charm(
                "consumer",
                {"control": CharmEndpoint(type=EndpointType.REQUIRES, interface="control")},
            ),
            domain,
            model_ref,
        )
        replacement_id = add_charm_to_domain(
            principal.model_copy(update={"ubuntu_version": "22.04", "revision": 2}),
            domain,
            model_ref,
        )
        domain.charms[principal_id].charms_added.append(replacement_id)
        pair_charms_in_domain(domain, principal_id, consumer_id)
        solver = z3.Solver()
        solver.add(
            domain.charms[principal_id].exists,
            domain.charms[subordinate_id].exists,
            domain.charms[consumer_id].exists,
            z3.Not(domain.charms[replacement_id].exists),
        )
        assert solver.check() == z3.sat

        # WHEN preparing replacement alternatives for optimization
        BundleBuilder(charmhub_client=_FakeCharmhubClient())._prepare_optimization_domain(domain, solver.model())

        # THEN the replacement can retain both the global and container-scoped neighbors
        assert any(
            {item.requires_charm_id, item.provides_charm_id} == {consumer_id, replacement_id}
            for item in domain.charm_integrations
        )
        assert any(
            {item.requires_charm_id, item.provides_charm_id} == {subordinate_id, replacement_id}
            for item in domain.charm_integrations
        )

    def test_active_replacement_is_not_paired_with_itself(self) -> None:
        # Regression test: a charm's own replacement can already be active too.
        # GIVEN a mesh-style charm with an active original and an active replacement
        domain = Domain()
        model_ref = ModelRef(name="m")
        domain.models[model_ref] = DomainModel(
            arch="amd64",
            platform="kubernetes",
            juju_version=_JUJU,
        )
        mesh_charm = _make_charm(
            "grafana-k8s",
            {
                "require-cmr-mesh": CharmEndpoint(type=EndpointType.REQUIRES, interface="cross_model_mesh"),
                "provide-cmr-mesh": CharmEndpoint(type=EndpointType.PROVIDES, interface="cross_model_mesh"),
            },
        )
        original_id = add_charm_to_domain(mesh_charm, domain, model_ref)
        replacement_id = add_charm_to_domain(mesh_charm.model_copy(update={"revision": 2}), domain, model_ref)
        domain.charms[original_id].charms_added.append(replacement_id)
        solver = z3.Solver()
        solver.add(domain.charms[original_id].exists, domain.charms[replacement_id].exists)
        assert solver.check() == z3.sat

        # WHEN preparing the active graph for optimization
        BundleBuilder(charmhub_client=_FakeCharmhubClient())._prepare_optimization_domain(domain, solver.model())

        # THEN no integration pairs the replacement with itself
        assert not any(item.requires_charm_id == item.provides_charm_id for item in domain.charm_integrations)

        # AND building constraints doesn't crash z3
        add_constraints(z3.Solver(), domain)


class TestSolve:
    """BundleBuilder._solve."""

    def test_returns_valid_solution(self) -> None:
        # GIVEN a domain with two charm alternatives
        domain, id_a, id_b = _domain_with_two_alternatives()
        # _solve resolves the spec's own charms up front to check they can be integrated,
        # so the client has to serve "myapp" as well.
        builder = BundleBuilder(charmhub_client=_FakeCharmhubClient(_make_charm_variant(revision=1, priority=1.0)))

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


class TestHandlePeerChannelMismatch:
    """BundleBuilder._handle_peer_channel_mismatch."""

    def test_variants_are_paired_with_the_original_counterpart(self) -> None:
        # GIVEN an anchor and peer whose channel-compatible variants are fetched
        domain = Domain()
        model_ref = ModelRef(name="m")
        domain.models[model_ref] = DomainModel(
            arch="amd64",
            platform="kubernetes",
            juju_version=_JUJU,
        )
        anchor = _make_charm(
            "anchor",
            {"ep": CharmEndpoint(type=EndpointType.PROVIDES, interface="mesh")},
        )
        peer = _make_charm(
            "peer",
            {"ep": CharmEndpoint(type=EndpointType.REQUIRES, interface="mesh")},
        )
        add_charm_to_domain(anchor, domain, model_ref)
        add_charm_to_domain(peer, domain, model_ref)
        peer_variant = peer.model_copy(update={"revision": 2})
        anchor_variant = anchor.model_copy(update={"revision": 2})
        builder = BundleBuilder(charmhub_client=_FakeCharmhubClient(charm_responses=[peer_variant, anchor_variant]))

        # WHEN resolving the mismatch in both directions
        result = builder._handle_peer_channel_mismatch(
            _mismatch(anchor_id=0, peer_id=1, track="latest"),
            domain,
        )

        # THEN each variant is paired with the original charm on the other side
        assert result is True
        pairs = {
            frozenset((integration.requires_charm_id, integration.provides_charm_id))
            for integration in domain.charm_integrations
        }
        assert frozenset((0, 2)) in pairs
        assert frozenset((1, 3)) in pairs

    def test_peer_in_different_model_gets_variant_placed_in_its_own_model(self) -> None:
        # GIVEN an anchor and peer in different models (a cross-model relation), each
        # with its own arch/juju/platform
        domain = Domain()
        anchor_model_ref = ModelRef(name="anchor-model", controller="anchor-controller")
        peer_model_ref = ModelRef(name="peer-model", controller="peer-controller")
        domain.models[anchor_model_ref] = DomainModel(
            arch="amd64",
            platform="kubernetes",
            juju_version=_JUJU,
        )
        domain.models[peer_model_ref] = DomainModel(
            arch="arm64",
            platform="kubernetes",
            juju_version=_JUJU,
        )
        anchor = _make_charm(
            "anchor",
            {"ep": CharmEndpoint(type=EndpointType.PROVIDES, interface="mesh")},
        )
        peer = _make_charm(
            "peer",
            {"ep": CharmEndpoint(type=EndpointType.REQUIRES, interface="mesh")},
        )
        add_charm_to_domain(anchor, domain, anchor_model_ref)
        add_charm_to_domain(peer, domain, peer_model_ref)
        peer_variant = peer.model_copy(update={"revision": 2})
        fake = _FakeCharmhubClient(charm_responses=[peer_variant, CharmReleaseNotFoundException("no match")])
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN resolving the mismatch
        result = builder._handle_peer_channel_mismatch(
            _mismatch(anchor_id=0, peer_id=1, track="latest"),
            domain,
        )

        # THEN the new peer variant is placed in the peer's own model, not the anchor's
        assert result is True
        peer_variant_id = next(
            cid for cid, charm in enumerate(domain.charms) if charm.spec.name == "peer" and charm.spec.revision == 2
        )
        assert domain.charms[peer_variant_id].model == peer_model_ref

        # AND the charm was looked up using the peer's own model attributes
        assert fake.charm_from_store_calls[0]["ubuntu_arch"] == "arm64"


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


def _domain_for_unresolved_diagnostics() -> Domain:
    """A domain with two applications ('target'/easyrsa, 'neighbor'/kafka) and no charms added."""
    domain = Domain()
    domain.models[ModelRef(name="m")] = DomainModel(
        arch="amd64",
        platform="machine",
        juju_version=_JUJU,
        applications={
            "target": DomainApplication(charm="easyrsa"),
            "neighbor": DomainApplication(charm="kafka"),
        },
    )
    return domain


class TestCollectUnsatDiagnostics:
    """BundleBuilder._collect_unsat_diagnostics."""

    def test_collect_unresolved_applications_resolves_charm_name_from_spec(self) -> None:
        # GIVEN a domain where 'neighbor' is declared (but never resolved) as charm 'kafka'
        domain = _domain_for_unresolved_diagnostics()
        app_exists = ApplicationExistsTag(model=ModelRef(name="m"), application="neighbor")

        # WHEN collecting unresolved-application diagnostics from the unsat core
        result = BundleBuilder._collect_unsat_diagnostics([app_exists], domain)

        # THEN the charm name is resolved from the spec, not left as the generic application name
        assert result == (UnresolvedApplicationDiagnostic(application="neighbor", charm_name="kafka"),)

    def test_collect_unresolved_integrations_resolves_charm_names_from_spec(self) -> None:
        # GIVEN a domain with 'target'/easyrsa and 'neighbor'/kafka, and an integration tag naming
        # an endpoint that doesn't exist on kafka
        domain = _domain_for_unresolved_diagnostics()
        integration_exists = ApplicationIntegrationExistsTag(
            model=ModelRef(name="m"),
            integration=[
                AppEndpointPayload(application="target", endpoint="client"),
                AppEndpointPayload(application="neighbor", endpoint="trusted-certificate"),
            ],
        )

        # WHEN collecting unresolved-integration diagnostics from the unsat core
        result = BundleBuilder._collect_unsat_diagnostics([integration_exists], domain)

        # THEN both endpoints are resolved to their charm names, not the generic application names
        assert result == (
            UnresolvedIntegrationDiagnostic(
                endpoints=(
                    DiagnosticEndpoint(application="target", endpoint="client", charm_name="easyrsa"),
                    DiagnosticEndpoint(
                        application="neighbor",
                        endpoint="trusted-certificate",
                        charm_name="kafka",
                    ),
                )
            ),
        )

    def test_collect_unresolved_application_falls_back_to_application_name_when_unknown(self) -> None:
        # GIVEN an unsat core naming a model/application that isn't present in the domain
        domain = _domain_for_unresolved_diagnostics()
        app_exists = ApplicationExistsTag(model=ModelRef(name="other-model"), application="mystery")

        # WHEN collecting unresolved-application diagnostics
        result = BundleBuilder._collect_unsat_diagnostics([app_exists], domain)

        # THEN the application name is used as a defensive fallback for the charm name
        assert result == (UnresolvedApplicationDiagnostic(application="mystery", charm_name="mystery"),)

    def test_collects_every_independent_diagnostic(self) -> None:
        domain = _domain_for_unresolved_diagnostics()
        non_optional = CharmEndpointNonOptionalTag(
            charm=CharmEndpointPayload(charm_name="postgresql", charm_id=0, endpoint="db"),
            interface="pgsql",
        )
        mismatch = IntegrationFeatureMismatchTag(
            requires=CharmEndpointPayload(
                charm_name="katib-controller",
                charm_id=1,
                endpoint="k8s-service-info",
            ),
            provides=CharmEndpointPayload(charm_name="kfp-viz", charm_id=2, endpoint="kfp-viz"),
            feature="katib-service",
        )

        result = BundleBuilder._collect_unsat_diagnostics([mismatch, non_optional], domain)

        assert result == (
            UnfulfilledEndpointDiagnostic(
                endpoint=DiagnosticEndpoint(charm_name="postgresql", endpoint="db"),
                interface="pgsql",
            ),
            FeatureMismatchDiagnostic(
                requires=DiagnosticEndpoint(
                    charm_name="katib-controller",
                    endpoint="k8s-service-info",
                ),
                provides=DiagnosticEndpoint(charm_name="kfp-viz", endpoint="kfp-viz"),
                feature="katib-service",
            ),
        )
        error = UncompletableBundleError(diagnostics=result)
        assert "postgresql:db" in str(error)
        assert "katib-service" in str(error)

    def test_release_failure_suppresses_redundant_application_and_integration(self) -> None:
        domain = _domain_for_unresolved_diagnostics()
        domain.models[ModelRef(name="other")] = DomainModel(
            arch="amd64",
            platform="machine",
            juju_version=_JUJU,
            applications={"neighbor": DomainApplication(charm="kafka-other")},
        )
        app_exists = ApplicationExistsTag(model=ModelRef(name="m"), application="neighbor")
        other_app_exists = ApplicationExistsTag(model=ModelRef(name="other"), application="neighbor")
        integration_exists = ApplicationIntegrationExistsTag(
            model=ModelRef(name="m"),
            integration=[
                AppEndpointPayload(application="target", endpoint="client"),
                AppEndpointPayload(application="neighbor", endpoint="trusted-certificate"),
            ],
        )
        release = ApplicationReleaseDiagnostic(
            application="neighbor",
            charm_name="kafka",
            model="m",
            error=CharmReleaseNotFoundException("No compatible release"),
        )

        result = BundleBuilder._collect_unsat_diagnostics(
            [app_exists, other_app_exists, integration_exists],
            domain,
            [release],
        )

        assert result == (
            UnresolvedApplicationDiagnostic(application="neighbor", charm_name="kafka-other"),
            release,
        )

    def test_unknown_tags_produce_internal_failure_diagnostic(self) -> None:
        domain = _domain_for_unresolved_diagnostics()
        peer_mismatch = PeerChannelMismatchTag(
            charm=CharmPayload(charm_name="mysql-router", charm_id=0),
            endpoint="db-router",
            peer_charm_name="mysql",
            peer_charm_id=1,
            required_track="8.0",
        )

        result = BundleBuilder._collect_unsat_diagnostics([peer_mismatch], domain)

        assert result == (
            BundleBuildFailureDiagnostic(
                kind=BundleBuildFailureKind.UNEXPANDABLE_ASSERTIONS,
                detail="Cannot expand the domain to handle failed assertion tags",
            ),
        )
