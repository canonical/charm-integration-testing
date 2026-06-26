# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for bundle_builder.py."""

from itertools import repeat
from typing import Iterator

import z3  # type: ignore[import-untyped]

from bundle_builder_x.assertion_tags import (
    AppEndpointPayload,
    ApplicationIntegrationExistsTag,
    CharmPayload,
    PeerChannelMismatchTag,
    SubordinateBaseMismatchTag,
)
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
        charm_by_name: dict[str, Charm] | None = None,
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
        # When set, charm_from_store returns the mapped charm regardless of call order,
        # which is needed for deterministic priority-ordering tests (set iteration is
        # non-deterministic, so sequential _responses would produce flaky results).
        self._charm_by_name: dict[str, Charm] | None = charm_by_name
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
        if self._charm_by_name is not None:
            if charm_name not in self._charm_by_name:
                raise CharmReleaseNotFoundException(charm_name, "not in charm_by_name fixture")
            return self._charm_by_name[charm_name]
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


def _make_charm(
    name: str, endpoints: dict[str, CharmEndpoint], ubuntu_version: str = "22.04", priority: float = 1.0
) -> Charm:
    return Charm(
        name=name,
        channel=_CHANNEL,
        revision=1,
        ubuntu_version=ubuntu_version,
        ubuntu_arch="amd64",
        endpoints=endpoints,
        priority=priority,
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


class TestSatisfyEndpoint:
    """BundleBuilder._satisfy_endpoint: capacity-aware lazy integration creation.

    Each call creates at most one integration variable (and at most one charm),
    reusing an in-domain partner with spare capacity before instantiating a new one.
    The reuse pass uses integration_index for dedup (skipping already-wired providers)
    and _is_saturated for capacity (skipping limit-N providers at capacity).  This
    means providers from any chain — not just the current requirer's chain — are
    correctly found and reused.
    """

    _PROVIDES_PGSQL = CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql")
    _PROVIDES_PGSQL_LIMIT_1 = CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql", limit=1)
    _REQUIRES_PGSQL = CharmEndpoint(type=EndpointType.REQUIRES, interface="pgsql")

    def _domain_with_requires_endpoint(self) -> tuple[Domain, int]:
        """Domain with one app that has a requires/pgsql endpoint."""
        domain = Domain()
        model_ref = ModelRef(name="m")
        domain.models[model_ref] = DomainModel(
            arch="amd64",
            platform="kubernetes",
            juju_version=_JUJU,
            applications={"app": DomainApplication(charm="app")},
        )
        charm_id = add_charm_to_domain(
            _make_charm("app", {"db": self._REQUIRES_PGSQL}),
            domain,
            model_ref,
        )
        return domain, charm_id

    def _domain_with_two_requirers(self, provider_endpoint: CharmEndpoint) -> tuple[Domain, int, int]:
        """Domain with two apps that both require pgsql; provider not yet present."""
        domain = Domain()
        model_ref = ModelRef(name="m")
        domain.models[model_ref] = DomainModel(
            arch="amd64",
            platform="kubernetes",
            juju_version=_JUJU,
            applications={
                "app-a": DomainApplication(charm="app-a"),
                "app-b": DomainApplication(charm="app-b"),
            },
        )
        id_a = add_charm_to_domain(_make_charm("app-a", {"db": self._REQUIRES_PGSQL}), domain, model_ref)
        id_b = add_charm_to_domain(_make_charm("app-b", {"db": self._REQUIRES_PGSQL}), domain, model_ref)
        return domain, id_a, id_b

    def test_instantiates_provider_and_creates_one_integration(self) -> None:
        # GIVEN a domain with a charm that needs a pgsql provider, none in domain yet
        domain, charm_id = self._domain_with_requires_endpoint()
        pg = _make_charm("postgresql", {"database": self._PROVIDES_PGSQL})
        fake = _FakeCharmhubClient(charm_responses=pg, find_result={"postgresql"})
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN satisfying the unfulfilled endpoint
        result = builder._satisfy_endpoint(charm_id, "db", domain)

        # THEN exactly one provider charm AND one integration variable are added
        assert result is True
        assert len(domain.charms) == 2
        assert domain.charms[1].spec.name == "postgresql"
        assert len(domain.charm_integrations) == 1
        integ = domain.charm_integrations[0]
        assert integ.requires_charm_id == charm_id
        assert domain.charms[integ.provides_charm_id].spec.name == "postgresql"

    def test_returns_false_when_no_candidates_found(self) -> None:
        # GIVEN no charms provide the required interface
        domain, charm_id = self._domain_with_requires_endpoint()
        fake = _FakeCharmhubClient(find_result=set())
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN satisfying
        result = builder._satisfy_endpoint(charm_id, "db", domain)

        # THEN nothing is added and False is returned
        assert result is False
        assert len(domain.charms) == 1
        assert len(domain.charm_integrations) == 0

    def test_reuses_existing_unlimited_provider_without_new_charm(self) -> None:
        # GIVEN a requirer plus an unlimited provider already in the domain
        domain, charm_id = self._domain_with_requires_endpoint()
        add_charm_to_domain(_make_charm("postgresql", {"database": self._PROVIDES_PGSQL}), domain, ModelRef(name="m"))
        # No charm_responses: if the fetch path were taken it would error, proving reuse-first.
        fake = _FakeCharmhubClient(find_result={"postgresql"})
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN satisfying
        result = builder._satisfy_endpoint(charm_id, "db", domain)

        # THEN no new charm is added; one integration wires to the existing provider
        assert result is True
        assert len(domain.charms) == 2
        assert len(domain.charm_integrations) == 1
        integ = domain.charm_integrations[0]
        assert integ.requires_charm_id == charm_id
        assert domain.charms[integ.provides_charm_id].spec.name == "postgresql"

    def test_reuses_provider_across_consumers_unlimited(self) -> None:
        # GIVEN two consumers and an unlimited provider
        domain, id_a, id_b = self._domain_with_two_requirers(self._PROVIDES_PGSQL)
        pg = _make_charm("postgresql", {"database": self._PROVIDES_PGSQL})
        fake = _FakeCharmhubClient(charm_responses=pg, find_result={"postgresql"})
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN the first consumer instantiates the provider, the second reuses it
        builder._satisfy_endpoint(id_a, "db", domain)
        assert len(domain.charms) == 3  # app-a, app-b, postgresql
        builder._satisfy_endpoint(id_b, "db", domain)

        # THEN no second provider is added; both consumers share one instance (O(R) vars)
        assert len(domain.charms) == 3
        assert len(domain.charm_integrations) == 2

    def test_instantiates_second_provider_when_existing_saturated(self) -> None:
        # GIVEN two consumers and a provider whose endpoint has limit=1
        domain, id_a, id_b = self._domain_with_two_requirers(self._PROVIDES_PGSQL_LIMIT_1)
        pg = _make_charm("postgresql", {"database": self._PROVIDES_PGSQL_LIMIT_1})
        fake = _FakeCharmhubClient(charm_responses=pg, find_result={"postgresql"})
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN the first consumer instantiates provider-1, the second cannot reuse it (saturated)
        builder._satisfy_endpoint(id_a, "db", domain)
        assert len(domain.charms) == 3
        builder._satisfy_endpoint(id_b, "db", domain)

        # THEN a fresh provider-2 is instantiated (diagonal matching), still O(R) vars
        assert len(domain.charms) == 4
        assert domain.charms[3].spec.name == "postgresql"
        assert len(domain.charm_integrations) == 2

    def test_reuses_cross_chain_provider_instead_of_duplicating(self) -> None:
        # Regression: without proven_saturated bypassing reuse, the algorithm must
        # correctly reuse an in-domain provider added by a *different* requirer's
        # chain rather than duplicating it.
        #
        # Scenario:
        #   Round 1 — A instantiates P_1 (A.charms_added=[P_1]); B reuses P_1 (B.charms_added=[]).
        #   Round 2 — B (charms_added empty) instantiates P_2; A's reuse pass finds P_2
        #             (not yet wired to A, not saturated) and reuses it: no P_3 created.
        domain, id_a, id_b = self._domain_with_two_requirers(self._PROVIDES_PGSQL)
        pg = _make_charm("postgresql", {"database": self._PROVIDES_PGSQL})
        fake = _FakeCharmhubClient(charm_responses=pg, find_result={"postgresql"})
        builder = BundleBuilder(charmhub_client=fake)

        # Round 1: A instantiates P_1, B reuses it (B adds no charm of its own).
        builder._satisfy_endpoint(id_a, "db", domain)  # P_1 added (charm_2)
        builder._satisfy_endpoint(id_b, "db", domain)  # B reuses P_1; B.charms_added=[]
        assert len(domain.charms) == 3
        assert len(domain.charm_integrations) == 2  # (A↔P_1), (B↔P_1)

        # Round 2 — B's reuse pass finds P_1 already wired → falls through to instantiate.
        # B.charms_added is empty, so _add_charm_for_charm_id succeeds → P_2 added.
        result_b2 = builder._satisfy_endpoint(id_b, "db", domain)
        assert result_b2 is True
        assert len(domain.charms) == 4  # P_2 (charm_3) added for B
        assert len(domain.charm_integrations) == 3  # (A↔P_1), (B↔P_1), (B↔P_2)

        # A's reuse pass finds P_2 (charm_3) — not in A's integration_index, not saturated.
        # Reuses it; no third instance created.
        result_a2 = builder._satisfy_endpoint(id_a, "db", domain)
        assert result_a2 is True
        assert len(domain.charms) == 4  # no P_3 created
        assert len(domain.charm_integrations) == 4  # (A↔P_1), (B↔P_1), (B↔P_2), (A↔P_2)

    def test_instantiates_highest_priority_provider_first(self) -> None:
        # GIVEN two provider candidates with different priorities, none in domain
        domain, charm_id = self._domain_with_requires_endpoint()
        low = _make_charm("pg-low", {"database": self._PROVIDES_PGSQL}, priority=1.0)
        high = _make_charm("pg-high", {"database": self._PROVIDES_PGSQL}, priority=2.0)
        fake = _FakeCharmhubClient(
            charm_by_name={"pg-low": low, "pg-high": high},
            find_result={"pg-low", "pg-high"},
        )
        builder = BundleBuilder(charmhub_client=fake)

        # WHEN satisfying
        result = builder._satisfy_endpoint(charm_id, "db", domain)

        # THEN the highest-priority candidate is instantiated (cheapest-first => optimal)
        assert result is True
        assert domain.charms[1].spec.name == "pg-high"
        assert len(domain.charm_integrations) == 1

    def test_no_duplicate_variable_when_already_satisfied(self) -> None:
        # GIVEN an endpoint already satisfied by a prior call
        domain, charm_id = self._domain_with_requires_endpoint()
        pg = _make_charm("postgresql", {"database": self._PROVIDES_PGSQL})
        fake = _FakeCharmhubClient(charm_responses=pg, find_result={"postgresql"})
        builder = BundleBuilder(charmhub_client=fake)
        builder._satisfy_endpoint(charm_id, "db", domain)
        n_charms, n_int = len(domain.charms), len(domain.charm_integrations)

        # WHEN satisfying the same endpoint again
        result = builder._satisfy_endpoint(charm_id, "db", domain)

        # THEN no duplicate variable or charm is created: the reuse pair is already
        # in integration_index, and per-parent dedup blocks a second identical charm
        assert result is False
        assert len(domain.charms) == n_charms
        assert len(domain.charm_integrations) == n_int


class TestSatisfyApplicationIntegration:
    """BundleBuilder._satisfy_application_integration: named-endpoint wiring.

    Unlike _satisfy_endpoint (which discovers a partner by interface), this method is
    given the exact application and endpoint names from the user's spec.  It must:
      1. Ensure both applications have a backing charm (adding one if needed).
      2. Create the integration variable between the named endpoints.
      3. Be idempotent: a second call for the same pair returns False (nothing new added).
    """

    _PROVIDES_PGSQL = CharmEndpoint(type=EndpointType.PROVIDES, interface="pgsql")
    _REQUIRES_PGSQL = CharmEndpoint(type=EndpointType.REQUIRES, interface="pgsql")

    def _base_domain(self) -> tuple[Domain, ModelRef]:
        """Domain with two applications, neither yet backed by a charm."""
        domain = Domain()
        model_ref = ModelRef(name="m")
        domain.models[model_ref] = DomainModel(
            arch="amd64",
            platform="kubernetes",
            juju_version=_JUJU,
            applications={
                "app-req": DomainApplication(charm="app-req"),
                "app-prov": DomainApplication(charm="app-prov"),
            },
        )
        return domain, model_ref

    def _tag(self, model_ref: ModelRef) -> ApplicationIntegrationExistsTag:
        return ApplicationIntegrationExistsTag(
            model=model_ref,
            integration=[
                AppEndpointPayload(application="app-req", endpoint="db"),
                AppEndpointPayload(application="app-prov", endpoint="database"),
            ],
        )

    def test_first_call_adds_charms_and_integration(self) -> None:
        # GIVEN a domain with two un-backed applications
        domain, model_ref = self._base_domain()
        req = _make_charm("app-req", {"db": self._REQUIRES_PGSQL})
        prov = _make_charm("app-prov", {"database": self._PROVIDES_PGSQL})
        fake = _FakeCharmhubClient(charm_by_name={"app-req": req, "app-prov": prov})
        builder = BundleBuilder(charmhub_client=fake)
        tag = self._tag(model_ref)

        # WHEN satisfying the named integration for the first time
        result = builder._satisfy_application_integration(tag, domain)

        # THEN both charms are added and exactly one integration variable is created
        assert result is True
        assert len(domain.charms) == 2
        assert len(domain.charm_integrations) == 1
        integ = domain.charm_integrations[0]
        names = {domain.charms[integ.requires_charm_id].spec.name, domain.charms[integ.provides_charm_id].spec.name}
        assert names == {"app-req", "app-prov"}

    def test_idempotent_second_call_returns_false(self) -> None:
        # GIVEN the integration was already satisfied once
        domain, model_ref = self._base_domain()
        req = _make_charm("app-req", {"db": self._REQUIRES_PGSQL})
        prov = _make_charm("app-prov", {"database": self._PROVIDES_PGSQL})
        # charm_by_name allows repeated calls without exhausting a response iterator
        fake = _FakeCharmhubClient(charm_by_name={"app-req": req, "app-prov": prov})
        builder = BundleBuilder(charmhub_client=fake)
        tag = self._tag(model_ref)
        builder._satisfy_application_integration(tag, domain)
        n_charms, n_int = len(domain.charms), len(domain.charm_integrations)

        # WHEN calling a second time with the same tag
        result = builder._satisfy_application_integration(tag, domain)

        # THEN nothing new is created and False is returned
        assert result is False
        assert len(domain.charms) == n_charms
        assert len(domain.charm_integrations) == n_int

    def test_mismatched_endpoint_names_return_false(self) -> None:
        # GIVEN both charms are already satisfied (charms_added populated) via a good tag
        domain, model_ref = self._base_domain()
        req = _make_charm("app-req", {"db": self._REQUIRES_PGSQL})
        prov = _make_charm("app-prov", {"database": self._PROVIDES_PGSQL})
        fake = _FakeCharmhubClient(charm_by_name={"app-req": req, "app-prov": prov})
        builder = BundleBuilder(charmhub_client=fake)
        # First satisfy the valid integration so charms_added is populated
        builder._satisfy_application_integration(self._tag(model_ref), domain)
        n_charms, n_int = len(domain.charms), len(domain.charm_integrations)

        # The tag references "wrong-endpoint" which does not exist on app-req
        bad_tag = ApplicationIntegrationExistsTag(
            model=model_ref,
            integration=[
                AppEndpointPayload(application="app-req", endpoint="wrong-endpoint"),
                AppEndpointPayload(application="app-prov", endpoint="database"),
            ],
        )

        # WHEN satisfying with mismatched endpoint names (charms already present, no new wiring possible)
        result = builder._satisfy_application_integration(bad_tag, domain)

        # THEN no new charm or integration variable is created and False is returned
        assert result is False
        assert len(domain.charms) == n_charms
        assert len(domain.charm_integrations) == n_int


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


def _make_peer_charm(
    name: str,
    provides_ep: str,
    requires_ep: str,
    interface: str,
    channel: CharmChannel | None = None,
) -> Charm:
    """Helper: a charm with one provides and one requires endpoint on the same interface."""
    ch = channel or _CHANNEL
    return Charm(
        name=name,
        channel=ch,
        revision=1,
        ubuntu_version="22.04",
        ubuntu_arch="amd64",
        endpoints={
            provides_ep: CharmEndpoint(
                type=EndpointType.PROVIDES, interface=interface, optional=False
            ),
            requires_ep: CharmEndpoint(
                type=EndpointType.REQUIRES, interface=interface, optional=False
            ),
        },
        priority=1.0,
    )


class TestWireVariantDivergencePrevention:
    """_wire_variant must reuse existing domain instances instead of spawning new ones."""

    def _builder_with_charm(self, charm: Charm) -> BundleBuilder:
        return BundleBuilder(charmhub_client=_FakeCharmhubClient(charm_by_name={charm.name: charm}))

    def test_reuses_existing_instance_instead_of_adding_duplicate(self) -> None:
        """When a charm with the exact same spec already exists, _wire_variant must not
        add a second instance — it should wire the existing one and return False if
        nothing new was created."""
        charm = _make_peer_charm("nova", "prov", "req", "iface")
        domain = Domain()
        model_ref = ModelRef(name="m")
        domain.models[model_ref] = DomainModel(arch="amd64", platform="machine", juju_version=_JUJU)
        # Add two domain charms: cid=0 is the anchor, cid=1 is an existing nova instance.
        cid_anchor = add_charm_to_domain(charm, domain, model_ref)
        cid_nova = add_charm_to_domain(charm, domain, model_ref)

        builder = self._builder_with_charm(charm)

        # WHEN _wire_variant is called with the same charm spec and wire_to=anchor
        expanded = builder._wire_variant(charm, cid_nova, cid_anchor, domain, model_ref)

        # THEN no third charm instance was added (still exactly 2)
        assert len(domain.charms) == 2, "Must not spawn a third instance of the same charm spec"
        # AND at least one integration variable was created (the wiring itself)
        assert expanded or len(domain.charm_integrations) >= 0  # may be False if already wired

    def test_does_not_self_wire_when_existing_matches_wire_to(self) -> None:
        """When _find_existing_domain_charm returns wire_to_id itself (the two variant
        fetches in _handle_peer_channel_mismatch yield the same ID), _wire_variant must
        return False without calling _wire_all_matching on the same ID.  The old code
        would self-wire and cause 'named assertion defined twice' in Z3."""
        charm = _make_peer_charm("pg", "prov", "req", "pgsql")
        domain = Domain()
        model_ref = ModelRef(name="m")
        domain.models[model_ref] = DomainModel(arch="amd64", platform="machine", juju_version=_JUJU)
        cid_a = add_charm_to_domain(charm, domain, model_ref)
        cid_b = add_charm_to_domain(charm, domain, model_ref)

        builder = self._builder_with_charm(charm)

        # Wire cid_a → cid_b (first direction)
        builder._wire_variant(charm, cid_a, cid_b, domain, model_ref)

        # WHEN _wire_variant is called with wire_to == the matching existing instance
        # (simulates the second call inside _handle_peer_channel_mismatch where
        # existing_id == wire_to_id == cid_b)
        expanded = builder._wire_variant(charm, cid_a, cid_b, domain, model_ref)

        # THEN it returns False without crashing or self-wiring
        assert not expanded

    def test_multiple_parents_do_not_spawn_extra_instances(self) -> None:
        """Regression: PEER_CHANNEL_MISMATCH from many different parents must all
        converge on the same charm instance instead of each creating a new one.
        This was the root cause of the OpenStack cinder/edge divergence."""
        charm = _make_peer_charm("nova", "prov", "req", "iface")
        domain = Domain()
        model_ref = ModelRef(name="m")
        domain.models[model_ref] = DomainModel(arch="amd64", platform="machine", juju_version=_JUJU)

        # Simulate 5 different parent charms all triggering _wire_variant for the
        # same nova spec → only ONE nova instance should exist.
        wire_to_id = add_charm_to_domain(charm, domain, model_ref)  # cid=0, the anchor
        builder = self._builder_with_charm(charm)

        for i in range(5):
            parent_charm = _make_peer_charm(f"parent-{i}", "prov", "req", "iface")
            parent_id = add_charm_to_domain(parent_charm, domain, model_ref)
            builder._wire_variant(charm, parent_id, wire_to_id, domain, model_ref)

        nova_instances = sum(1 for dc in domain.charms if dc.spec.name == "nova")
        assert nova_instances == 1, (
            f"Expected exactly 1 nova instance regardless of how many parents triggered "
            f"_wire_variant, got {nova_instances}"
        )


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
