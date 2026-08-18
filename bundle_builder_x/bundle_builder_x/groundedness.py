# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Static detection of specs that no bundle can satisfy.

The CEGIS loop in :mod:`bundle_builder_x.bundle_builder` expands the domain whenever
z3 reports unsat, and its only termination condition is the per-iteration solver
timeout.  On a spec that is genuinely unsatisfiable the loop therefore never
converges: each expansion adds another charm instance, z3 reports unsat again, and
the build ends in a timeout that says nothing about what is wrong.  Raising the
timeout does not help, because the domain simply grows larger before hitting it.

This module recognises the structural cases up front, so they are reported as
unsatisfiable instead of running until the clock expires.

Why the check is valid
----------------------
:func:`bundle_builder_x.constraints.add_charm_dependency_constraints` gives every
active integration a strict rank ordering, ``rank[requirer] > rank[provider]``.  Rank
is therefore a strict order on a finite bundle, so a chain of non-optional REQUIRES
edges cannot close into a cycle -- it must bottom out at a charm whose own
non-optional requires are already satisfied.

Let ``F(S)`` be the set of charms all of whose non-optional, non-cyclic REQUIRES
endpoints have some provider in ``S``.  ``F`` is monotone, so it has a least
fixpoint, and by induction on rank every charm appearing in a valid bundle lies in
it.  The dual fixpoint over PROVIDES endpoints -- which need a requirer of strictly
higher rank -- follows the same way.  A charm named in the spec that lies outside the
relevant fixpoint cannot appear in any valid bundle, so the spec is unsatisfiable.

The check is sound but deliberately incomplete: it proves unsatisfiability for this
structural class only, and stays silent about every other reason a spec might fail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .charm import JUJU_INFO_INTERFACE, Charm, CharmEndpoint, EndpointType
from .charmhub import CharmhubClient
from .charmhub_http import CharmReleaseNotFoundException, UnparsableCharmException
from .domain import Domain, DomainApplication, DomainModel, ModelRef
from .juju_version import JujuVersion

# Upper bound on charms downloaded while proving unsatisfiability. On a spec large
# enough to exhaust it the check abstains and the builder behaves as it did before.
EXPLORE_BUDGET = 2000

_MAX_REPORTED_PROBLEMS = 5


class _BudgetExhausted(Exception):
    """Exploration hit EXPLORE_BUDGET, so no verdict can be reached."""


@dataclass
class _Memo:
    """Groundedness answers cached across every endpoint of a single check."""

    grounded: set[str] = field(default_factory=set)
    ungrounded: set[str] = field(default_factory=set)


class _Catalog:
    """Memoised view of Charmhub covering every platform used by the domain.

    A charm counts as available if it can be fetched for *any* platform in the spec.
    That over-approximates what is really deployable, which is the direction that
    keeps the verdict sound: the check may fail to prove unsatisfiability, but it
    cannot wrongly claim it.
    """

    def __init__(self, client: CharmhubClient, domain: Domain) -> None:
        self._client = client
        # Every (platform, arch, juju version) the domain deploys on. A charm that
        # resolves under any of them is treated as available: arch and Juju version
        # are per-model, and juju_version additionally gates `assumes`, so testing
        # only the first model would hide providers that other models can use.
        self._targets = list({(m.platform, m.arch, m.juju_version) for m in domain.models.values()})
        self._platforms = sorted({platform for platform, _, _ in self._targets})
        self._partners: dict[tuple[str, str], list[str]] = {}
        self._charms: dict[str, Charm | None] = {}
        # Seed applications' own resolved charms. find_charms is filtered by the
        # `listed` override (see e.g. static/charm-overrides/cinder.yaml), which
        # excludes charms not meant for *automated discovery* -- but a seed
        # application is never discovered, it is named explicitly in the spec, so
        # it can still be the real-world partner for another seed's endpoint (e.g.
        # cinder-lvm:storage-backend is only ever wired to an explicitly named
        # cinder, never one the builder finds on its own). Registering seeds here
        # makes them visible to partners() regardless of `listed`.
        self._seeds: list[Charm] = []

    def register_seed(self, charm: Charm) -> None:
        self._seeds.append(charm)

    def seed_charm(self, model: DomainModel, app: DomainApplication) -> Charm | None:
        """Resolve a spec application's charm exactly as the builder will resolve it.

        Seed applications may pin a channel, revision or base, and endpoint overrides
        are channel-dependent, so the pins have to be honoured here.  Reading a
        different release could show an obligation the pinned one does not have, and
        the check would then reject a spec the builder can solve.  Mirrors
        BundleBuilder._get_charm_for_application.
        """
        try:
            return self._client.charm_from_store(
                charm_name=app.charm,
                ubuntu_arch=model.arch,
                charm_track=app.channel.track if app.channel else None,
                charm_risk=app.channel.risk if app.channel else None,
                charm_revision=app.revision,
                ubuntu_version=app.base,
                juju_version=model.juju_version,
                platform=model.platform,
            )
        except (CharmReleaseNotFoundException, UnparsableCharmException):
            return None

    def charm(self, name: str) -> Charm | None:
        """Return a candidate partner charm's metadata, or None if unavailable.

        Partners carry no pins, matching how the builder discovers them.
        """
        if name in self._charms:
            return self._charms[name]
        if len(self._charms) >= EXPLORE_BUDGET:
            raise _BudgetExhausted
        found: Charm | None = None
        for platform, arch, juju_version in self._targets:
            found = self._fetch(name, platform, arch, juju_version)
            if found is not None:
                break
        self._charms[name] = found
        return found

    def _fetch(self, name: str, platform: str, arch: str, juju_version: JujuVersion | None) -> Charm | None:
        try:
            return self._client.charm_from_store(
                charm_name=name,
                ubuntu_arch=arch,
                juju_version=juju_version,
                platform=platform,
            )
        except (CharmReleaseNotFoundException, UnparsableCharmException):
            return None

    def partners(self, endpoint: CharmEndpoint) -> list[str]:
        """Return the charms that could sit on the other side of `endpoint`."""
        counterpart = _COUNTERPART[endpoint.type]
        side = "provides" if endpoint.type == EndpointType.REQUIRES else "requires"
        key = (side, endpoint.interface)
        if key not in self._partners:
            names: list[str] = []
            for platform in self._platforms:
                for name in self._client.find_charms(**{side: endpoint.interface}, platform=platform):
                    if name not in names:
                        names.append(name)
            for seed in self._seeds:
                if seed.name not in names and any(
                    ep.type == counterpart and ep.interface == endpoint.interface for ep in seed.endpoints.values()
                ):
                    names.append(seed.name)
            self._partners[key] = names
        return self._partners[key]

    @property
    def explored(self) -> int:
        return len(self._charms)


_COUNTERPART = {EndpointType.REQUIRES: EndpointType.PROVIDES, EndpointType.PROVIDES: EndpointType.REQUIRES}


def _is_obligation(endpoint: CharmEndpoint, direction: EndpointType) -> bool:
    """True if `endpoint` must be integrated, and by a partner that has to bottom out.

    juju-info is excluded because every machine charm provides it implicitly without
    declaring it (see CharmhubClient.find_charms), so the obligation is always
    dischargeable and the partner set is the entire catalogue.
    """
    return (
        endpoint.type == direction
        and not endpoint.optional
        and not endpoint.cyclic
        and endpoint.interface != JUJU_INFO_INTERFACE
    )


def _obligations(charm: Charm, direction: EndpointType) -> list[CharmEndpoint]:
    """Endpoints of `charm` in `direction` that must be integrated to deploy it."""
    return [ep for ep in charm.endpoints.values() if _is_obligation(ep, direction)]


def _closes_cycle(charm: Charm, obligation: CharmEndpoint) -> bool:
    """True if `charm` can discharge `obligation` with an edge that carries no rank ordering.

    add_charm_dependency_constraints skips the rank assertion when *either* side of an
    integration is cyclic, so such an edge may close a cycle.  The obligation is then
    discharged outright and nothing beyond it has to bottom out -- which is exactly
    what the repo's own overrides encode for pairs like temporal-k8s/temporal-ui-k8s.
    """
    counterpart = _COUNTERPART[obligation.type]
    return any(
        ep.type == counterpart and ep.interface == obligation.interface and ep.cyclic for ep in charm.endpoints.values()
    )


def _screen(catalog: _Catalog, endpoint: CharmEndpoint, direction: EndpointType, memo: _Memo) -> bool:
    """Linear under-approximation of "some partner of `endpoint` is grounded".

    Recurses only until one grounded partner is found, so a healthy spec costs the
    seed charms plus roughly one partner per endpoint, however many alternatives
    Charmhub offers.

    Charms on the current recursion stack count as not-yet-grounded, which is what
    makes this a *least* fixpoint: a charm can only be grounded by bottoming out,
    never by depending on itself.  Negative answers are therefore conditional on the
    stack, and memoising them is what keeps the search linear rather than
    exponential -- at the cost of turning it into an under-approximation.  A negative
    result must be confirmed with :func:`_exact` before it is acted on.
    """

    def discharged(obligation: CharmEndpoint, stack: set[str]) -> bool:
        for name in catalog.partners(obligation):
            charm = catalog.charm(name)
            if charm is None:
                continue
            if _closes_cycle(charm, obligation) or visit(name, charm, stack):
                return True
        return False

    def visit(name: str, charm: Charm, stack: set[str]) -> bool:
        if name in memo.grounded:
            return True
        if name in memo.ungrounded or name in stack:
            return False
        stack.add(name)
        try:
            ok = all(discharged(obligation, stack) for obligation in _obligations(charm, direction))
        finally:
            stack.discard(name)
        (memo.grounded if ok else memo.ungrounded).add(name)
        return ok

    return discharged(endpoint, set())


def _exact(catalog: _Catalog, seeds: list[Charm], direction: EndpointType) -> tuple[set[str], set[str]]:
    """Exact least fixpoint of groundedness over every partner reachable from `seeds`.

    Returns the grounded charm names, plus the interfaces on which some reachable
    partner offers a cyclic endpoint -- an obligation on one of those is dischargeable
    without bottoming out, so it is satisfied unconditionally.

    Reached only once the screen has flagged something, i.e. when a spec is about to
    be rejected, so a healthy build never pays for the exhaustive exploration.

    Seed charms are deliberately absent from the result: they are pinned to a
    specific release and are keyed by name, which two models could disagree on.
    Callers decide a seed endpoint by asking whether any of its partners is grounded,
    which is exactly the question :func:`_screen` answers.
    """
    reachable: dict[str, Charm] = {}
    pending: list[Charm] = list(seeds)
    seen: set[str] = set()
    while pending:
        for obligation in _obligations(pending.pop(), direction):
            for partner in catalog.partners(obligation):
                if partner in seen:
                    continue
                seen.add(partner)
                charm = catalog.charm(partner)
                if charm is not None:
                    reachable[partner] = charm
                    pending.append(charm)

    # Index by the endpoint type that can satisfy an obligation in `direction`.
    counterpart = _COUNTERPART[direction]
    providers: dict[str, set[str]] = {}
    unranked: set[str] = set()
    for name, charm in reachable.items():
        for endpoint in charm.endpoints.values():
            if endpoint.type != counterpart:
                continue
            providers.setdefault(endpoint.interface, set()).add(name)
            if endpoint.cyclic:
                unranked.add(endpoint.interface)

    grounded: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, charm in reachable.items():
            if name in grounded:
                continue
            if all(
                obligation.interface in unranked
                or any(partner in grounded for partner in providers.get(obligation.interface, ()))
                for obligation in _obligations(charm, direction)
            ):
                grounded.add(name)
                changed = True
    return grounded, unranked


def _external_cmr_endpoints(domain: Domain) -> set[tuple[str, str]]:
    """Local (application, endpoint) pairs wired to a model outside the domain.

    add_charm_constraints adds a constant term to such an endpoint's count, so it can
    reach its required count with no in-domain partner at all.  Groundedness reasons
    only about in-domain charms, so these endpoints have to be left alone.  Mirrors
    the cmr_counts construction in constraints.add_charm_constraints.
    """
    external: set[tuple[str, str]] = set()
    for model in domain.models.values():
        for integration in model.application_integrations:
            model_1, model_2 = integration.endpoint_1.model, integration.endpoint_2.model
            if model_1 == model_2:
                continue  # local integration
            if (model_1 if model_1 != ModelRef() else model_2) in domain.models:
                continue  # in-domain CMR, whose count flows through integration.exists
            local = integration.endpoint_1 if model_1 == ModelRef() else integration.endpoint_2
            external.add((local.application, local.endpoint))
    return external


def _describe(charm_name: str, endpoint_name: str, endpoint: CharmEndpoint, application: str, orphan: bool) -> str:
    where = f"{charm_name}:{endpoint_name} (interface '{endpoint.interface}', application '{application}')"
    role, chain = ("providing", "provider") if endpoint.type == EndpointType.REQUIRES else ("requiring", "requirer")
    if orphan:
        return f"{where} can never be satisfied: no charm {role} '{endpoint.interface}' is available"
    return (
        f"{where} can never be satisfied: every charm {role} '{endpoint.interface}' has a non-optional "
        f"endpoint of its own that leads back to it, so no {chain} chain can terminate"
    )


def find_unsatisfiable_endpoints(client: CharmhubClient, domain: Domain, logger: logging.Logger) -> list[str]:
    """Describe every spec endpoint that provably cannot be integrated.

    An empty list means no proof was found, which is not the same as the spec being
    satisfiable -- see the module docstring.
    """
    catalog = _Catalog(client, domain)
    memo = _Memo()
    external = _external_cmr_endpoints(domain)
    problems: list[str] = []
    seeds: list[Charm] = []
    suspects: list[tuple[Charm, str, CharmEndpoint, str]] = []

    try:
        seed_endpoints: list[tuple[Charm, str, CharmEndpoint, str]] = []
        for model in domain.models.values():
            for application, app in model.applications.items():
                charm = catalog.seed_charm(model, app)
                if charm is None:
                    # An unavailable charm is reported by the normal expansion path.
                    continue
                seeds.append(charm)
                catalog.register_seed(charm)
                for endpoint_name, endpoint in charm.endpoints.items():
                    seed_endpoints.append((charm, endpoint_name, endpoint, application))

        # Screening happens only once every seed is registered, so a seed's own
        # endpoint can be found as a partner for another seed's obligation
        # regardless of which one appears first in the spec (see register_seed).
        for charm, endpoint_name, endpoint, application in seed_endpoints:
            if (application, endpoint_name) in external:
                continue
            if endpoint.type == EndpointType.PEERS:
                if not endpoint.optional and not endpoint.cyclic:
                    # pair_charms_in_domain only ever pairs REQUIRES with PROVIDES, so a
                    # non-optional peer endpoint never gets an integration variable and can
                    # never reach its required count. If peer relations are ever modelled
                    # properly, this branch must be removed with them.
                    problems.append(
                        f"{charm.name}:{endpoint_name} (interface '{endpoint.interface}', "
                        f"application '{application}') is a non-optional peer endpoint, "
                        f"which the solver can never integrate"
                    )
            elif _is_obligation(endpoint, endpoint.type) and not _screen(catalog, endpoint, endpoint.type, memo):
                suspects.append((charm, endpoint_name, endpoint, application))

        for direction in {endpoint.type for _, _, endpoint, _ in suspects}:
            grounded, unranked = _exact(catalog, seeds, direction)
            for charm, endpoint_name, endpoint, application in suspects:
                if endpoint.type != direction or endpoint.interface in unranked:
                    continue
                partners = catalog.partners(endpoint)
                if any(partner in grounded for partner in partners):
                    continue
                problems.append(_describe(charm.name, endpoint_name, endpoint, application, orphan=not partners))
    except _BudgetExhausted:
        logger.info(f"Groundedness check explored {EXPLORE_BUDGET} charms without a verdict; skipping")
        return []

    logger.debug(f"Groundedness check explored {catalog.explored} charms and found {len(problems)} problems")
    return sorted(set(problems))


def format_problems(problems: list[str]) -> str:
    """Render problems for an error message, capping how many are listed."""
    shown = problems[:_MAX_REPORTED_PROBLEMS]
    suffix = f" (and {len(problems) - len(shown)} more)" if len(problems) > len(shown) else ""
    return "; ".join(shown) + suffix
