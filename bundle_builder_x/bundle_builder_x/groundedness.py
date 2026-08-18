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

from .charm import Charm, CharmEndpoint, EndpointType
from .charmhub import CharmhubClient
from .charmhub_http import CharmReleaseNotFoundException, UnparsableCharmException
from .domain import Domain

# Upper bound on charms downloaded while proving unsatisfiability. On a spec large
# enough to exhaust it the check abstains and the builder behaves as it did before.
EXPLORE_BUDGET = 2000

_MAX_REPORTED_PROBLEMS = 5


class _BudgetExhausted(Exception):
    """Exploration hit EXPLORE_BUDGET, so no verdict can be reached."""


class _Catalog:
    """Memoised view of Charmhub covering every platform used by the domain.

    A charm counts as available if it can be fetched for *any* platform in the spec.
    That over-approximates what is really deployable, which is the direction that
    keeps the verdict sound: the check may fail to prove unsatisfiability, but it
    cannot wrongly claim it.
    """

    def __init__(self, client: CharmhubClient, domain: Domain) -> None:
        self._client = client
        models = list(domain.models.values())
        self._platforms = sorted({model.platform for model in models})
        self._arch = models[0].arch
        self._juju_version = models[0].juju_version
        self._partners: dict[tuple[str, str], list[str]] = {}
        # Seed the cache from the domain: DomainBuilder has already resolved the
        # spec's own charms, so re-fetching them here would be a wasted round trip.
        self._charms: dict[str, Charm | None] = {charm.spec.name: charm.spec for charm in domain.charms}

    def charm(self, name: str) -> Charm | None:
        """Return the charm's metadata, or None if it is unavailable everywhere."""
        if name in self._charms:
            return self._charms[name]
        if len(self._charms) >= EXPLORE_BUDGET:
            raise _BudgetExhausted
        found: Charm | None = None
        for platform in self._platforms:
            found = self._fetch(name, platform)
            if found is not None:
                break
        self._charms[name] = found
        return found

    def _fetch(self, name: str, platform: str) -> Charm | None:
        try:
            return self._client.charm_from_store(
                charm_name=name,
                ubuntu_arch=self._arch,
                juju_version=self._juju_version,
                platform=platform,
            )
        except (CharmReleaseNotFoundException, UnparsableCharmException):
            return None

    def partners(self, endpoint: CharmEndpoint) -> list[str]:
        """Return the charms that could sit on the other side of `endpoint`."""
        side = "provides" if endpoint.type == EndpointType.REQUIRES else "requires"
        key = (side, endpoint.interface)
        if key not in self._partners:
            names: list[str] = []
            for platform in self._platforms:
                for name in self._client.find_charms(**{side: endpoint.interface}, platform=platform):
                    if name not in names:
                        names.append(name)
            self._partners[key] = names
        return self._partners[key]

    @property
    def explored(self) -> int:
        return len(self._charms)


def _obligations(charm: Charm, direction: EndpointType) -> list[CharmEndpoint]:
    """Endpoints of `charm` in `direction` that must be integrated to deploy it."""
    return [ep for ep in charm.endpoints.values() if ep.type == direction and not ep.optional and not ep.cyclic]


def _screen(catalog: _Catalog, name: str, direction: EndpointType) -> bool:
    """Linear under-approximation of groundedness for `name`.

    Recurses only until one grounded partner is found per obligation, so a healthy
    spec costs a handful of fetches however many alternatives Charmhub offers.

    Charms on the current recursion stack count as not-yet-grounded, which is what
    makes this a *least* fixpoint: a charm can only be grounded by bottoming out,
    never by depending on itself.  Negative answers are therefore conditional on the
    stack, and memoising them is what makes the search linear rather than
    exponential -- at the cost of turning it into an under-approximation.  Callers
    must confirm a negative result with :func:`_exact`.
    """
    grounded: set[str] = set()
    ungrounded: set[str] = set()

    def visit(name: str, stack: set[str]) -> bool:
        if name in grounded:
            return True
        if name in ungrounded or name in stack:
            return False
        charm = catalog.charm(name)
        if charm is None:
            return False
        stack.add(name)
        try:
            ok = all(
                any(visit(partner, stack) for partner in catalog.partners(endpoint))
                for endpoint in _obligations(charm, direction)
            )
        finally:
            stack.discard(name)
        (grounded if ok else ungrounded).add(name)
        return ok

    return visit(name, set())


def _exact(catalog: _Catalog, domain: Domain, direction: EndpointType) -> set[str]:
    """Exact least fixpoint over the whole catalogue reachable from the spec.

    Only reached once the screen has flagged something, i.e. when a spec is about to
    be rejected, so the exhaustive exploration is never paid for by a healthy build.
    """
    reachable: dict[str, Charm] = {}
    pending = [app.charm for model in domain.models.values() for app in model.applications.values()]
    seen = set(pending)
    while pending:
        name = pending.pop()
        charm = catalog.charm(name)
        if charm is None:
            continue
        reachable[name] = charm
        for endpoint in _obligations(charm, direction):
            for partner in catalog.partners(endpoint):
                if partner not in seen:
                    seen.add(partner)
                    pending.append(partner)

    providers: dict[str, set[str]] = {}
    counterpart = EndpointType.PROVIDES if direction == EndpointType.REQUIRES else EndpointType.REQUIRES
    for name, charm in reachable.items():
        for endpoint in charm.endpoints.values():
            if endpoint.type == counterpart:
                providers.setdefault(endpoint.interface, set()).add(name)

    grounded: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, charm in reachable.items():
            if name in grounded:
                continue
            if all(
                any(partner in grounded for partner in providers.get(endpoint.interface, ()))
                for endpoint in _obligations(charm, direction)
            ):
                grounded.add(name)
                changed = True
    return grounded


def _describe(charm_name: str, endpoint_name: str, endpoint: CharmEndpoint, application: str) -> str:
    where = f"{charm_name}:{endpoint_name} (interface '{endpoint.interface}', application '{application}')"
    if endpoint.type == EndpointType.REQUIRES:
        return (
            f"{where} can never be satisfied: every charm providing '{endpoint.interface}' itself "
            f"non-optionally requires an interface that leads back to it, so no provider chain can terminate"
        )
    return (
        f"{where} can never be satisfied: every charm requiring '{endpoint.interface}' itself "
        f"non-optionally provides an interface that leads back to it, so no requirer chain can terminate"
    )


def find_unsatisfiable_endpoints(client: CharmhubClient, domain: Domain, logger: logging.Logger) -> list[str]:
    """Describe every spec endpoint that provably cannot be integrated.

    An empty list means no proof was found, which is not the same as the spec being
    satisfiable -- see the module docstring.
    """
    catalog = _Catalog(client, domain)
    problems: list[str] = []
    suspects: list[tuple[str, str, CharmEndpoint, str]] = []

    try:
        for model in domain.models.values():
            for application, app in model.applications.items():
                charm = catalog.charm(app.charm)
                if charm is None:
                    # An unavailable charm is reported by the normal expansion path.
                    continue
                for endpoint_name, endpoint in charm.endpoints.items():
                    if endpoint.optional or endpoint.cyclic:
                        continue
                    if endpoint.type == EndpointType.PEERS:
                        # pair_charms_in_domain only ever pairs REQUIRES with PROVIDES, so a
                        # non-optional peer endpoint never gets an integration variable and can
                        # never reach its required count. If peer relations are ever modelled
                        # properly, this branch must be removed with them.
                        problems.append(
                            f"{app.charm}:{endpoint_name} (interface '{endpoint.interface}', "
                            f"application '{application}') is a non-optional peer endpoint, "
                            f"which the solver can never integrate"
                        )
                    elif not _screen(catalog, app.charm, endpoint.type):
                        suspects.append((app.charm, endpoint_name, endpoint, application))

        for direction in {endpoint.type for _, _, endpoint, _ in suspects}:
            grounded = _exact(catalog, domain, direction)
            problems.extend(
                _describe(charm_name, endpoint_name, endpoint, application)
                for charm_name, endpoint_name, endpoint, application in suspects
                if endpoint.type == direction and charm_name not in grounded
            )
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
