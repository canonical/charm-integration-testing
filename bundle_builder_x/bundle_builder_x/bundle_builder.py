# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import timedelta
from typing import cast

import z3  # type: ignore[import-untyped]
from pydantic import BaseModel

from .assertion_tags import (
    ApplicationExistsTag,
    ApplicationIntegrationExistsTag,
    Assertions,
    AssertionTag,
    CharmEndpointNonOptionalTag,
    EndpointCountMatchesIntegrationsTag,
    PeerChannelMismatchTag,
    SubordinateBaseMismatchTag,
)
from .bundle import Solution
from .charm import Charm, CharmChannel, EndpointScope, EndpointType
from .charmhub import CharmhubClient
from .charmhub_http import CharmReleaseNotFoundException
from .constraints import add_constraints
from .domain import (
    Domain,
    ModelRef,
    add_charm_to_domain,
    pair_charms_in_domain,
)
from .domain_builder import DomainBuilder
from .extract import extract_solution
from .snapstore import SnapstoreClient
from .spec import SpecFile
from .timing import NullTimeline, Timeline

_DEFAULT_OPTIMIZE_TIMEOUT = timedelta(minutes=1)

_COST_SCALE = 1_000_000
_COST_EPSILON = 1e-6

_EXPANSION_PRIORITY: dict[Assertions, int] = {
    Assertions.APPLICATION_EXISTS: 0,
    Assertions.APPLICATION_INTEGRATION_EXISTS: 1,
    Assertions.CHARM_ENDPOINT_NON_OPTIONAL: 2,
    Assertions.ENDPOINT_COUNT_MATCHES_INTEGRATIONS: 3,
    Assertions.PEER_CHANNEL_MISMATCH: 4,
    Assertions.SUBORDINATE_BASE_MISMATCH: 5,
}


class UnfulfilledEndpointInfo(BaseModel):
    """Information about a charm endpoint that could not be fulfilled during bundle building."""

    charm_name: str
    endpoint: str
    interface: str | None


class UncompletableBundleError(ValueError):
    """Exception raised when bundle builder cannot generate a complete bundle from the base bundle.

    NOTE: This is the canonical failure exception for the bundle builder.
    Raise it directly with a descriptive ``reason`` for any condition that prevents producing
    a complete, valid bundle. Do not add new exception types unless callers must
    programmatically distinguish that specific case.
    """

    unsat_core: list[AssertionTag]

    def __init__(
        self,
        reason: str | None = None,
        unsat_core: list[AssertionTag] | None = None,
    ):
        self.unsat_core = unsat_core or []
        if reason is None:
            if self.unfulfilled_endpoints:
                reason = f"Cannot fulfill charm endpoints: {', '.join(f'{ep.charm_name}:{ep.endpoint}' for ep in sorted(self.unfulfilled_endpoints, key=lambda e: (e.charm_name, e.endpoint)))}"
            else:
                reason = "Cannot expand domain to handle failed assertion tags"
        super().__init__(f"Could not build a complete valid bundle: {reason}")

    @property
    def unfulfilled_endpoints(self) -> list[UnfulfilledEndpointInfo]:
        """Endpoints in the unsat core that could not be fulfilled by any charm."""
        return [
            UnfulfilledEndpointInfo(
                charm_name=cast(CharmEndpointNonOptionalTag, tag).charm.charm_name,
                endpoint=cast(CharmEndpointNonOptionalTag, tag).charm.endpoint,
                interface=cast(CharmEndpointNonOptionalTag, tag).interface,
            )
            for tag in self.unsat_core
            if tag.kind == Assertions.CHARM_ENDPOINT_NON_OPTIONAL
        ]


class BundleBuilder:
    charmhub_client: CharmhubClient
    snapstore_client: SnapstoreClient
    logger: logging.Logger
    timeline: Timeline
    optimize_timeout: timedelta
    _domain_builder: DomainBuilder

    def __init__(
        self,
        charmhub_client: CharmhubClient,
        snapstore_client: SnapstoreClient | None = None,
        logger: logging.Logger = logging.getLogger(__name__),
        timeline: Timeline | None = None,
        optimize_timeout: timedelta = _DEFAULT_OPTIMIZE_TIMEOUT,
    ):
        self.charmhub_client = charmhub_client
        self.snapstore_client = snapstore_client if snapstore_client is not None else SnapstoreClient(logger=logger)
        self.logger = logger
        self.timeline = (timeline if timeline is not None else NullTimeline()).child("builder")
        self.optimize_timeout = optimize_timeout
        self._domain_builder = DomainBuilder(self.snapstore_client, logger=self.logger)

    def build(self, spec: SpecFile) -> Solution:
        """Build bundles for all models defined in a spec simultaneously."""
        self._validate_platforms(spec)
        domain = self._domain_builder.build(spec)
        z3_model = self._solve(domain)
        return extract_solution(z3_model, domain, logger=self.logger)

    def _validate_platforms(self, spec: SpecFile) -> None:
        """Ensure every application is placed on a model whose platform its overrides allow.

        A charm's platform overrides (when present) enumerate the platforms it is
        expected to run on. Placing such a charm on a model with a different platform
        cannot produce a deployable bundle, so fail fast with a clear error.
        """
        overrides_client = self.charmhub_client.overrides_client
        for model_spec in spec.models:
            for application, app_spec in model_spec.applications.items():
                supported_platforms = overrides_client.get_charm_platform_overrides(app_spec.charm)
                if supported_platforms is not None:
                    supported_platforms = supported_platforms or ["machine"]
                    if model_spec.platform not in supported_platforms:
                        raise UncompletableBundleError(
                            f"Charm {app_spec.charm!r} (model={model_spec.key!r}, application={application!r}) "
                            f"supports platform(s) {supported_platforms!r}, but was placed on a model with "
                            f"platform {model_spec.platform!r}."
                        )

    def _solve(self, domain: Domain) -> z3.ModelRef:
        # Iterative CEGIS loop: expand domain until satisfiable.
        # Termination relies on the per-iteration solver timeout; there is no hard
        # iteration cap so that arbitrarily complex dependency graphs can converge.
        iteration = 0
        while True:
            iteration += 1
            self.logger.info(f"Iteration {iteration}")

            # Create solver with unsat core tracking and a per-iteration timeout
            solver = z3.Solver()
            solver.set("unsat_core", True)
            solver.set("timeout", int(self.optimize_timeout.total_seconds() * 1000))

            # Add constraints
            t_constraints = self.timeline.on(f"iter{iteration}/add_constraints")
            add_constraints(solver, domain)
            self.timeline.off(t_constraints)

            # Check satisfiability
            t_check = self.timeline.on(f"iter{iteration}/solve")
            result = solver.check()
            self.timeline.off(t_check)

            if result == z3.sat:
                self.logger.info("Problem is satisfiable")
                model = solver.model()
                self.logger.info("Re-solving with optimization to minimize applications and integrations")
                t_opt = self.timeline.on(f"iter{iteration}/optimize")
                try:
                    model = self._optimize_solution(domain, initial_model=model)
                finally:
                    self.timeline.off(t_opt)
                return model

            elif result == z3.unsat:
                self.logger.info("Problem is unsatisfiable; expanding domain")

                unsat_core = solver.unsat_core()
                if len(unsat_core) == 0:
                    self.logger.warning("Solver returned unsat but unsat core was empty")
                    raise UncompletableBundleError("Solver returned unsat but unsat core was empty")

                self._handle_unsat_core(unsat_core, domain)
            else:
                raise UncompletableBundleError(
                    f"Solver timed out after {self.optimize_timeout} at iteration {iteration}; "
                    "the domain may be too large to solve"
                )

    def _handle_unsat_core(self, unsat_core: z3.AstVector, domain: Domain) -> None:
        tags: list[AssertionTag] = sorted(
            self._merge_mismatch_tags([AssertionTag.decode(str(a)) for a in unsat_core]),
            key=lambda a: (_EXPANSION_PRIORITY.get(a.kind, len(_EXPANSION_PRIORITY)), str(a)),
        )
        expanded = False
        for tag in tags:
            self.logger.debug(f"Unsat core item: {tag}")
            if self._handle_failed_assertion(tag, domain):
                self.logger.info(f"Expanded domain to handle failed assertion tag: {tag}")
                expanded = True
        if not expanded:
            raise UncompletableBundleError(unsat_core=tags)

    @staticmethod
    def _merge_mismatch_tags(tags: list[AssertionTag]) -> list[AssertionTag]:
        """Merge PeerChannelMismatchTag pairs with the same (anchor, peer) into one tag.

        Track and risk constraints emit separate tags per dimension.  Merging them
        ensures _handle_peer_channel_mismatch resolves both in a single CEGIS step,
        avoiding a wrong intermediate channel on the first pass.
        """
        seen: dict[tuple[int, int], int] = {}
        merged: list[AssertionTag] = []
        for tag in tags:
            if isinstance(tag, PeerChannelMismatchTag):
                pair = (tag.charm.charm_id, tag.peer_charm_id)
                if pair in seen:
                    existing = cast(PeerChannelMismatchTag, merged[seen[pair]])
                    merged[seen[pair]] = PeerChannelMismatchTag(
                        charm=existing.charm,
                        endpoint=existing.endpoint,
                        peer_charm_name=existing.peer_charm_name,
                        peer_charm_id=existing.peer_charm_id,
                        required_track=existing.required_track or tag.required_track,
                        required_risk=existing.required_risk or tag.required_risk,
                        required_channel=existing.required_channel or tag.required_channel,
                        required_revision=existing.required_revision or tag.required_revision,
                    )
                else:
                    seen[pair] = len(merged)
                    merged.append(tag)
            else:
                merged.append(tag)
        return merged

    def _handle_failed_assertion(
        self,
        tag: AssertionTag,
        domain: Domain,
    ) -> bool:
        if tag.kind == Assertions.CHARM_ENDPOINT_NON_OPTIONAL:
            non_optional = cast(CharmEndpointNonOptionalTag, tag)
            return self._expand_for_endpoint(non_optional.charm.charm_id, non_optional.charm.endpoint, domain)

        elif tag.kind == Assertions.APPLICATION_EXISTS:
            app_exists = cast(ApplicationExistsTag, tag)
            model_ref = app_exists.model
            charm = self._get_charm_for_application(app_exists.application, domain, model_ref)
            return self._add_charm_for_application(charm, app_exists.application, domain, model_ref)

        elif tag.kind == Assertions.APPLICATION_INTEGRATION_EXISTS:
            app_integration_exists = cast(ApplicationIntegrationExistsTag, tag)
            results = []
            for endpoint in app_integration_exists.integration:
                model_ref = endpoint.model if endpoint.model.name is not None else app_integration_exists.model
                charm = self._get_charm_for_application(endpoint.application, domain, model_ref)
                results.append(self._add_charm_for_application(charm, endpoint.application, domain, model_ref))
            return any(results)

        elif tag.kind == Assertions.ENDPOINT_COUNT_MATCHES_INTEGRATIONS:
            count_tag = cast(EndpointCountMatchesIntegrationsTag, tag)
            return self._expand_for_endpoint(count_tag.charm.charm_id, count_tag.charm.endpoint, domain)

        elif tag.kind == Assertions.PEER_CHANNEL_MISMATCH:
            mismatch = cast(PeerChannelMismatchTag, tag)
            return self._handle_peer_channel_mismatch(mismatch, domain)

        elif tag.kind == Assertions.SUBORDINATE_BASE_MISMATCH:
            base_mismatch = cast(SubordinateBaseMismatchTag, tag)
            return self._handle_subordinate_base_mismatch(base_mismatch, domain)

        return False

    def _expand_for_endpoint(
        self,
        charm_id: int,
        endpoint_name: str,
        domain: Domain,
    ) -> bool:
        """Expand the domain to satisfy an unfulfilled endpoint.

        Priority order:
        1. Pair the endpoint charm with any already-in-domain charm that can satisfy
           it but hasn't been connected yet (no new charm vars introduced).
        2. If nothing existing can help, add the single highest-priority new fulfilling
           charm and connect it.

        Tries the owning model first, then other models (for cross-model integrations).
        """
        owning_model = domain.charms[charm_id].model

        # Step 1: connect any existing domain charm that fulfils the endpoint.
        if self._connect_existing_for_endpoint(charm_id, endpoint_name, domain, owning_model):
            return True
        for other_model_ref in domain.models:
            if other_model_ref == owning_model:
                continue
            if self._connect_existing_for_endpoint(charm_id, endpoint_name, domain, other_model_ref):
                return True

        # Step 2: no existing charm helped — add the best new candidate.
        charms = sorted(
            self._get_charms_for_endpoint(charm_id, endpoint_name, domain, owning_model),
            key=lambda c: c.priority,
            reverse=True,
        )
        if not charms:
            self.logger.debug(
                f"No charms found for endpoint {domain.charms[charm_id].spec.name}:{endpoint_name} "
                f"in model '{owning_model.key}'"
            )
        for charm in charms:
            if self._add_charm_for_charm_id(charm, charm_id, domain, owning_model):
                return True

        for other_model_ref in domain.models:
            if other_model_ref == owning_model:
                continue
            other_charms = sorted(
                self._get_charms_for_endpoint(charm_id, endpoint_name, domain, other_model_ref),
                key=lambda c: c.priority,
                reverse=True,
            )
            for charm in other_charms:
                if self._add_charm_for_charm_id(charm, charm_id, domain, other_model_ref):
                    return True

        return False

    def _connect_existing_for_endpoint(
        self,
        charm_id: int,
        endpoint_name: str,
        domain: Domain,
        target_model: ModelRef,
    ) -> bool:
        """Pair charm_id with any existing domain charm that can satisfy endpoint_name.

        Only creates integration variables — never adds new charms.  Returns True if
        at least one new integration variable was created.
        """
        endpoint = domain.charms[charm_id].spec.endpoints[endpoint_name]
        created_any = False
        for other_id, other_charm in enumerate(domain.charms):
            if other_id == charm_id:
                continue
            if other_charm.model != target_model:
                continue
            # Check this other charm has a compatible endpoint.
            for other_ep_name, other_ep in other_charm.spec.endpoints.items():
                if other_ep.interface != endpoint.interface:
                    continue
                if endpoint.type == EndpointType.REQUIRES and other_ep.type != EndpointType.PROVIDES:
                    continue
                if endpoint.type == EndpointType.PROVIDES and other_ep.type != EndpointType.REQUIRES:
                    continue
                if pair_charms_in_domain(domain, charm_id, other_id):
                    self.logger.debug(
                        f"Connected existing charm {other_charm.spec.name}:{other_id} "
                        f"to {domain.charms[charm_id].spec.name}:{charm_id} via {endpoint_name}"
                    )
                    created_any = True
                break  # pair_charms_in_domain handles all compatible endpoint pairs at once
        return created_any

    def _handle_peer_channel_mismatch(
        self,
        tag: PeerChannelMismatchTag,
        domain: Domain,
    ) -> bool:
        """Expand the domain by fetching a peer charm variant on the required channel."""
        owning_model = domain.charms[tag.charm.charm_id].model
        model = domain.models[owning_model]
        peer_channel = domain.charms[tag.peer_charm_id].spec.channel
        if tag.required_channel is not None:
            resolved = CharmChannel.model_validate(tag.required_channel)
            track: str | None = resolved.track or peer_channel.track or None
            risk: str | None = resolved.risk or None
        else:
            track = tag.required_track or peer_channel.track or None
            risk = tag.required_risk or None
        expanded = False

        # Try fetching the peer charm at the required channel.
        try:
            peer_charm = self.charmhub_client.charm_from_store(
                charm_name=tag.peer_charm_name,
                ubuntu_arch=model.arch,
                juju_version=model.juju_version,
                platform=model.platform,
                charm_track=track,
                charm_risk=risk,
                charm_revision=tag.required_revision,
            )
            expanded |= self._add_charm_for_charm_id(peer_charm, tag.peer_charm_id, domain, owning_model)
        except CharmReleaseNotFoundException:
            self.logger.debug(f"No release found for {tag.peer_charm_name} on {track}/{risk or '*'}")

        # Also try fetching the owning charm at the peer's actual channel so they can match.
        # This handles the case where the peer is pinned and the owning charm must adapt instead.
        try:
            owning_charm = self.charmhub_client.charm_from_store(
                charm_name=tag.charm.charm_name,
                ubuntu_arch=model.arch,
                juju_version=model.juju_version,
                platform=model.platform,
                charm_track=peer_channel.track,
                charm_risk=peer_channel.risk,
            )
            expanded |= self._add_charm_for_charm_id(owning_charm, tag.charm.charm_id, domain, owning_model)
        except CharmReleaseNotFoundException:
            self.logger.debug(
                f"No release found for {tag.charm.charm_name} on {peer_channel.track}/{peer_channel.risk or '*'}"
            )

        return expanded

    def _handle_subordinate_base_mismatch(
        self,
        tag: SubordinateBaseMismatchTag,
        domain: Domain,
    ) -> bool:
        """Expand the domain by fetching charm variants to resolve a subordinate/principal base mismatch.

        Attempts both directions: fetching the subordinate at the principal's base, and
        fetching the principal at the subordinate's base.
        """
        # Subordinate and principal must be in the same model (container-scoped
        # relations are cross-model-incompatible), so one model_ref covers both.
        model_ref = domain.charms[tag.subordinate_charm_id].model
        model = domain.models[model_ref]
        sub_charm_spec = domain.charms[tag.subordinate_charm_id].spec
        principal_base = tag.principal_base
        expanded = False

        # Try fetching the subordinate charm at the principal's base.
        try:
            sub_charm = self.charmhub_client.charm_from_store(
                charm_name=tag.subordinate_charm_name,
                ubuntu_arch=model.arch,
                juju_version=model.juju_version,
                platform=model.platform,
                charm_track=sub_charm_spec.channel.track or None,
                charm_risk=sub_charm_spec.channel.risk or None,
                ubuntu_version=principal_base,
            )
            expanded |= self._add_charm_for_charm_id(sub_charm, tag.subordinate_charm_id, domain, model_ref)
        except CharmReleaseNotFoundException:
            self.logger.debug(
                f"No release found for subordinate {tag.subordinate_charm_name} " f"on base {principal_base}"
            )

        # Also try fetching the principal at the subordinate's base, in case
        # the principal has a variant that matches.
        try:
            principal_spec = domain.charms[tag.principal_charm_id].spec
            principal_charm = self.charmhub_client.charm_from_store(
                charm_name=tag.principal_charm_name,
                ubuntu_arch=model.arch,
                juju_version=model.juju_version,
                platform=model.platform,
                charm_track=principal_spec.channel.track or None,
                charm_risk=principal_spec.channel.risk or None,
                ubuntu_version=tag.subordinate_base,
            )
            expanded |= self._add_charm_for_charm_id(principal_charm, tag.principal_charm_id, domain, model_ref)
        except CharmReleaseNotFoundException:
            self.logger.debug(
                f"No release found for principal {tag.principal_charm_name} " f"on base {tag.subordinate_base}"
            )

        return expanded

    def _get_charm_for_application(self, application: str, domain: Domain, model_ref: ModelRef) -> Charm:
        # Get the charm matching the application constraints
        model = domain.models[model_ref]
        app = model.applications[application]
        return self.charmhub_client.charm_from_store(
            charm_name=app.charm,
            ubuntu_arch=model.arch,
            charm_track=app.channel.track if app.channel else None,
            charm_risk=app.channel.risk if app.channel else None,
            charm_revision=app.revision,
            ubuntu_version=app.base,
            juju_version=model.juju_version,
            platform=model.platform,
        )

    def _get_charms_for_endpoint(
        self,
        charm_id: int,
        endpoint_name: str,
        domain: Domain,
        target_model: ModelRef,
    ) -> list[Charm]:
        """Find charms that can fulfill an endpoint, compatible with target_model's platform/arch."""
        model = domain.models[target_model]
        endpoint = domain.charms[charm_id].spec.endpoints[endpoint_name]

        # Container-scoped endpoints are machine-only; subordinates can't span models
        # or run on non-machine platforms, so skip querying Charmhub immediately.
        if endpoint.scope == EndpointScope.CONTAINER and model.platform != "machine":
            return []

        fulfilling_charms: set[str] = set()
        if endpoint.type == EndpointType.REQUIRES:
            fulfilling_charms = self.charmhub_client.find_charms(provides=endpoint.interface, platform=model.platform)
        elif endpoint.type == EndpointType.PROVIDES:
            fulfilling_charms = self.charmhub_client.find_charms(requires=endpoint.interface, platform=model.platform)

        # For container-scoped endpoints the other charm must share the same base
        ubuntu_version: str | None = None
        if endpoint.scope == EndpointScope.CONTAINER:
            ubuntu_version = domain.charms[charm_id].spec.ubuntu_version

        results: list[Charm] = []
        for charm_name in fulfilling_charms:
            try:
                results.append(
                    self.charmhub_client.charm_from_store(
                        charm_name=charm_name,
                        ubuntu_arch=model.arch,
                        juju_version=model.juju_version,
                        platform=model.platform,
                        ubuntu_version=ubuntu_version,
                    )
                )
            except CharmReleaseNotFoundException:
                self.logger.debug(f"Skipping {charm_name}: no compatible release for {model.platform}/{model.arch}")
        return results

    def _add_charm_for_application(
        self,
        charm: Charm,
        application: str,
        domain: Domain,
        model_ref: ModelRef,
    ) -> bool:
        model = domain.models[model_ref]
        domain_app = model.applications[application]
        # Check if this charm has already been added for this application
        for charm_id in domain_app.charms_added:
            if domain.charms[charm_id].spec == charm:
                return False

        # Add charm to domain
        self.logger.debug(f"Adding charm {charm.name} to model '{model_ref.key}' for application {application}")
        charm_id = add_charm_to_domain(charm, domain, model_ref)

        # Record that this charm was added for this application
        domain_app.charms_added.append(charm_id)
        return True

    def _add_charm_for_charm_id(
        self,
        charm: Charm,
        charm_id: int,
        domain: Domain,
        model_ref: ModelRef,
    ) -> bool:
        # Check if this exact charm was already added for this charm_id
        parent_charm = domain.charms[charm_id]
        for added_charm_id in parent_charm.charms_added:
            if domain.charms[added_charm_id].spec == charm:
                return False

        # Traverse the dependency chain to detect cycles
        # Walk backwards from charm_id through parents to see if the charm we're trying to add
        # already exists in the dependency chain (which would create a cycle)
        stack = [charm_id]
        visited = set()

        while stack:
            ancestor_id = stack.pop()
            if ancestor_id in visited:
                continue
            visited.add(ancestor_id)

            # If the charm we're trying to add is already this ancestor charm, it would create a cycle
            if domain.charms[ancestor_id].spec == charm:
                return False

            # Continue traversing: find parents that added this ancestor
            for pid, parent in enumerate(domain.charms):
                if ancestor_id in parent.charms_added:
                    stack.append(pid)

        # Add charm to domain
        self.logger.debug(
            f"Adding charm {charm.name} to model '{model_ref.key}' "
            f"for charm {domain.charms[charm_id].spec.name}:{charm_id}"
        )
        new_charm_id = add_charm_to_domain(charm, domain, model_ref)

        # Record that this charm was added for this charm_id
        parent_charm.charms_added.append(new_charm_id)
        return True

    @staticmethod
    def _build_cost_exprs(domain: Domain) -> tuple[z3.ExprRef, z3.ExprRef, z3.ExprRef]:
        """Build cost z3 expressions for a domain: charm cost, integration count, total num_units."""
        charm_cost_expr = z3.Sum(
            [
                z3.If(
                    charm.exists,
                    z3.IntVal(int(_COST_SCALE / max(charm.spec.priority, _COST_EPSILON))),
                    z3.IntVal(0),
                )
                for charm in domain.charms
            ]
            + [z3.IntVal(0)]
        )
        integration_cost_expr = z3.Sum(
            [z3.If(i.exists, 2 if domain.is_cross_model(i) else 1, 0) for i in domain.charm_integrations]
            + [z3.IntVal(0)]
        )
        # Sum num_units for existing charms so the optimizer picks the minimum satisfying
        # unit count rather than an arbitrary integer >= 1.
        num_units_cost_expr = z3.Sum(
            [z3.If(charm.exists, charm.num_units, z3.IntVal(0)) for charm in domain.charms] + [z3.IntVal(0)]
        )
        return charm_cost_expr, integration_cost_expr, num_units_cost_expr

    def _optimize_solution(
        self,
        domain: Domain,
        initial_model: z3.ModelRef | None = None,
        extra_constraints: list[z3.BoolRef] | None = None,
    ) -> z3.ModelRef:
        """Find the minimum cost model; tries z3.Optimize, falls back to iterative descent."""
        timeout_ms = int(self.optimize_timeout.total_seconds() * 1000)
        charm_cost_expr, integration_cost_expr, num_units_cost_expr = self._build_cost_exprs(domain)

        model = self._try_z3_optimize(
            domain, charm_cost_expr, integration_cost_expr, num_units_cost_expr, timeout_ms, extra_constraints
        )
        if model is not None:
            return model

        self.logger.warning("z3.Optimize timed out; falling back to iterative descent")
        return self._iterative_descent(
            domain, charm_cost_expr, integration_cost_expr, num_units_cost_expr, initial_model, extra_constraints
        )

    def _try_z3_optimize(
        self,
        domain: Domain,
        charm_cost_expr: z3.ExprRef,
        integration_cost_expr: z3.ExprRef,
        num_units_cost_expr: z3.ExprRef,
        timeout_ms: int,
        extra_constraints: list[z3.BoolRef] | None,
    ) -> z3.ModelRef | None:
        """Run z3.Optimize; return the model on sat, None on timeout, raise on unsat."""
        opt = z3.Optimize()
        opt.set("timeout", timeout_ms)
        add_constraints(opt, domain)
        for c in extra_constraints or []:
            opt.add(c)
        opt.minimize(charm_cost_expr)
        opt.minimize(integration_cost_expr)
        opt.minimize(num_units_cost_expr)
        result = opt.check()
        if result == z3.sat:
            self.logger.info("z3.Optimize found optimal solution")
            return opt.model()
        if result == z3.unsat:
            raise UncompletableBundleError("Optimization failed - problem became unsatisfiable")
        return None

    def _iterative_descent(
        self,
        domain: Domain,
        charm_cost_expr: z3.ExprRef,
        integration_cost_expr: z3.ExprRef,
        num_units_cost_expr: z3.ExprRef,
        initial_model: z3.ModelRef | None,
        extra_constraints: list[z3.BoolRef] | None,
    ) -> z3.ModelRef:
        """Minimize cost via three-phase SAT descent (charm cost, integration count, unit count).

        Each step gets the full optimize_timeout because each check is independently
        hard; a tight per-step budget would cause premature timeouts before the solver
        can prove the bound is tight (unsat).
        """
        step_ms = int(self.optimize_timeout.total_seconds() * 1000)

        def eval_charm_cost(m: z3.ModelRef) -> int:
            return sum(
                int(_COST_SCALE / max(c.spec.priority, _COST_EPSILON))
                for c in domain.charms
                if z3.is_true(m.eval(c.exists, model_completion=True))
            )

        def eval_integration_cost(m: z3.ModelRef) -> int:
            return sum(
                (2 if domain.is_cross_model(i) else 1)
                for i in domain.charm_integrations
                if z3.is_true(m.eval(i.exists, model_completion=True))
            )

        def eval_units_cost(m: z3.ModelRef) -> int:
            return sum(
                m.eval(c.num_units, model_completion=True).as_long()
                for c in domain.charms
                if z3.is_true(m.eval(c.exists, model_completion=True))
            )

        # Build the solver once; push/pop bound constraints on top each step.
        solver = z3.Solver()
        solver.set("timeout", step_ms)
        add_constraints(solver, domain)
        for c in extra_constraints or []:
            solver.add(c)

        # Seed from the caller's model to skip a redundant SAT solve.
        if initial_model is not None:
            model = initial_model
        else:
            result = solver.check()
            if result == z3.unsat:
                raise UncompletableBundleError("Optimization failed - problem became unsatisfiable")
            if result != z3.sat:
                raise UncompletableBundleError("Optimization failed - initial solve timed out")
            model = solver.model()

        # Phase 1: minimize charm cost.
        iterations = 0
        while True:
            current_cost = eval_charm_cost(model)
            solver.push()
            solver.add(charm_cost_expr < z3.IntVal(current_cost))
            result = solver.check()
            if result == z3.sat:
                model = solver.model()
                solver.pop()
                iterations += 1
                self.logger.debug(f"Optimize step {iterations}: charm cost {current_cost} -> {eval_charm_cost(model)}")
            elif result == z3.unsat:
                solver.pop()
                self.logger.info(f"Optimal charm cost found: {current_cost} ({iterations} descent step(s))")
                break
            else:
                solver.pop()
                self.logger.warning("Optimizer timed out during charm cost minimization; result may not be optimal")
                break

        # Phase 2: fix charm cost, minimize integration count.
        final_charm_cost = eval_charm_cost(model)
        iterations = 0
        while True:
            current_int_cost = eval_integration_cost(model)
            solver.push()
            solver.add(charm_cost_expr == z3.IntVal(final_charm_cost))
            solver.add(integration_cost_expr < z3.IntVal(current_int_cost))
            result = solver.check()
            if result == z3.sat:
                model = solver.model()
                solver.pop()
                iterations += 1
                self.logger.debug(
                    f"Optimize step {iterations}: integration cost {current_int_cost} -> {eval_integration_cost(model)}"
                )
            elif result == z3.unsat:
                solver.pop()
                self.logger.info(f"Optimal integration cost found: {current_int_cost} ({iterations} descent step(s))")
                break
            else:
                solver.pop()
                self.logger.warning("Optimizer timed out during integration cost minimization; charm count is optimal")
                break

        # Phase 3: fix charm and integration costs, minimize total unit count.
        final_int_cost = eval_integration_cost(model)
        iterations = 0
        while True:
            current_units_cost = eval_units_cost(model)
            solver.push()
            solver.add(charm_cost_expr == z3.IntVal(final_charm_cost))
            solver.add(integration_cost_expr == z3.IntVal(final_int_cost))
            solver.add(num_units_cost_expr < z3.IntVal(current_units_cost))
            result = solver.check()
            if result == z3.sat:
                model = solver.model()
                solver.pop()
                iterations += 1
                self.logger.debug(
                    f"Optimize step {iterations}: unit cost {current_units_cost} -> {eval_units_cost(model)}"
                )
            elif result == z3.unsat:
                solver.pop()
                self.logger.info(f"Optimal unit count found: {current_units_cost} ({iterations} descent step(s))")
                break
            else:
                solver.pop()
                self.logger.warning("Optimizer timed out during unit count minimization; integration count is optimal")
                break

        return model
