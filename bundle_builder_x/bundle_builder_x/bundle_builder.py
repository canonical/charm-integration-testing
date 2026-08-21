# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

import z3  # type: ignore[import-untyped]

from .assertion_tags import (
    ApplicationExistsTag,
    ApplicationIntegrationExistsTag,
    Assertions,
    AssertionTag,
    CharmEndpointNonOptionalTag,
    EndpointCountMatchesIntegrationsTag,
    IntegrationFeatureMismatchTag,
    PeerChannelMismatchTag,
    SubordinateBaseMismatchTag,
)
from .bundle import Solution
from .bundle_diagnostics import (
    ApplicationReleaseDiagnostic,
    BundleBuildFailureDiagnostic,
    BundleBuildFailureKind,
    BundleDiagnostic,
    DiagnosticEndpoint,
    FeatureMismatchDiagnostic,
    UnfulfilledEndpointDiagnostic,
    UnresolvedApplicationDiagnostic,
    UnresolvedIntegrationDiagnostic,
    canonicalize_diagnostics,
)
from .charm import Charm, CharmChannel, CharmEndpoint, EndpointScope, EndpointType
from .charmhub import CharmhubClient
from .constraints import add_constraints
from .domain import (
    Domain,
    ModelRef,
    add_charm_to_domain,
    pair_charms_in_domain,
)
from .domain_builder import DomainBuilder
from .extract import extract_solution
from .release_errors import CharmReleaseNotFoundException
from .snapstore import SnapstoreClient
from .spec import SpecFile
from .timing import NullTimeline, Timeline

_DEFAULT_OPTIMIZE_TIMEOUT = timedelta(minutes=1)
# CEGIS expands obligations from the selected SAT model; this regression-tested seed keeps
# that choice repeatable across runs.
_SOLVER_RANDOM_SEED = 6

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


@dataclass(frozen=True)
class AssertionHandlingResult:
    """Outcome of attempting to expand one failed solver assertion."""

    expanded: bool = False
    diagnostics: tuple[ApplicationReleaseDiagnostic, ...] = ()


class UncompletableBundleError(ValueError):
    """Exception raised when bundle builder cannot generate a complete bundle from the base bundle.

    This is the canonical failure exception for the bundle builder. Typed diagnostics carry
    the structured reasons.
    """

    diagnostics: tuple[BundleDiagnostic, ...]

    def __init__(self, diagnostics: Iterable[BundleDiagnostic]) -> None:
        self.diagnostics = canonicalize_diagnostics(diagnostics)
        if not self.diagnostics:
            raise ValueError("UncompletableBundleError requires at least one diagnostic")
        reason = "; ".join(diagnostic.description for diagnostic in self.diagnostics)
        super().__init__(f"Could not build a complete valid bundle: {reason}")


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
        domain = self._domain_builder.build(spec)
        z3_model = self._solve(domain)
        return extract_solution(z3_model, domain, logger=self.logger)

    def _solve(self, domain: Domain) -> z3.ModelRef:
        # Iterative CEGIS loop: expand domain until satisfiable.
        # Termination otherwise relies on the per-iteration solver timeout; there is no
        # hard iteration cap so that arbitrarily complex dependency graphs can converge.
        iteration = 0
        while True:
            iteration += 1
            self.logger.info(f"Iteration {iteration}")

            # Create solver with unsat core tracking and a per-iteration timeout
            solver = z3.Solver()
            solver.set("unsat_core", True)
            solver.set("timeout", int(self.optimize_timeout.total_seconds() * 1000))
            solver.set("random_seed", _SOLVER_RANDOM_SEED)

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
                self._prepare_optimization_domain(domain, model)
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
                    raise UncompletableBundleError(
                        diagnostics=(
                            BundleBuildFailureDiagnostic(
                                kind=BundleBuildFailureKind.EMPTY_UNSAT_CORE,
                                detail="Solver returned unsat but the unsat core was empty",
                            ),
                        )
                    )

                self._handle_unsat_core(unsat_core, domain)
            else:
                raise UncompletableBundleError(
                    diagnostics=(
                        BundleBuildFailureDiagnostic(
                            kind=BundleBuildFailureKind.SOLVER_TIMEOUT,
                            detail=(
                                f"Solver timed out after {self.optimize_timeout} at iteration {iteration}; "
                                "the domain may be too large to solve"
                            ),
                        ),
                    )
                )

    def _handle_unsat_core(self, unsat_core: z3.AstVector, domain: Domain) -> None:
        tags: list[AssertionTag] = sorted(
            self._merge_mismatch_tags([AssertionTag.decode(str(a)) for a in unsat_core]),
            key=lambda a: (_EXPANSION_PRIORITY.get(a.kind, len(_EXPANSION_PRIORITY)), str(a)),
        )
        expanded = False
        provisional_diagnostics: list[ApplicationReleaseDiagnostic] = []
        for tag in tags:
            self.logger.debug(f"Unsat core item: {tag}")
            result = self._handle_failed_assertion(tag, domain)
            provisional_diagnostics.extend(result.diagnostics)
            if result.expanded:
                self.logger.info(f"Expanded domain to handle failed assertion tag: {tag}")
                expanded = True
        if not expanded:
            raise UncompletableBundleError(
                diagnostics=self._collect_unsat_diagnostics(tags, domain, provisional_diagnostics),
            )

    @staticmethod
    def _application_charm_name(domain: Domain, model_ref: ModelRef, application: str) -> str:
        """Look up the spec-declared charm name for an application (available even if never resolved)."""
        model = domain.models.get(model_ref)
        if model is not None and application in model.applications:
            return model.applications[application].charm
        return application

    @classmethod
    def _collect_unsat_diagnostics(
        cls,
        tags: list[AssertionTag],
        domain: Domain,
        provisional_diagnostics: Iterable[ApplicationReleaseDiagnostic] = (),
    ) -> tuple[BundleDiagnostic, ...]:
        """Translate a final unsat core into canonical public diagnostics."""
        diagnostics: list[BundleDiagnostic] = list(provisional_diagnostics)
        release_failed_applications = {
            (diagnostic.model, diagnostic.application)
            for diagnostic in diagnostics
            if isinstance(diagnostic, ApplicationReleaseDiagnostic)
        }
        for tag in tags:
            if tag.kind == Assertions.CHARM_ENDPOINT_NON_OPTIONAL:
                non_optional = cast(CharmEndpointNonOptionalTag, tag)
                diagnostics.append(
                    UnfulfilledEndpointDiagnostic(
                        endpoint=DiagnosticEndpoint(
                            charm_name=non_optional.charm.charm_name,
                            endpoint=non_optional.charm.endpoint,
                        ),
                        interface=non_optional.interface,
                    )
                )
            elif tag.kind == Assertions.INTEGRATION_FEATURE_MISMATCH:
                mismatch = cast(IntegrationFeatureMismatchTag, tag)
                diagnostics.append(
                    FeatureMismatchDiagnostic(
                        requires=DiagnosticEndpoint(
                            charm_name=mismatch.requires.charm_name,
                            endpoint=mismatch.requires.endpoint,
                        ),
                        provides=DiagnosticEndpoint(
                            charm_name=mismatch.provides.charm_name,
                            endpoint=mismatch.provides.endpoint,
                        ),
                        feature=mismatch.feature,
                    ),
                )
            elif tag.kind == Assertions.APPLICATION_EXISTS:
                app_exists = cast(ApplicationExistsTag, tag)
                if (app_exists.model.key, app_exists.application) not in release_failed_applications:
                    diagnostics.append(
                        UnresolvedApplicationDiagnostic(
                            application=app_exists.application,
                            charm_name=cls._application_charm_name(
                                domain,
                                app_exists.model,
                                app_exists.application,
                            ),
                        )
                    )
            elif tag.kind == Assertions.APPLICATION_INTEGRATION_EXISTS:
                integration_exists = cast(ApplicationIntegrationExistsTag, tag)
                if any(
                    (
                        (endpoint.model if endpoint.model.name is not None else integration_exists.model).key,
                        endpoint.application,
                    )
                    in release_failed_applications
                    for endpoint in integration_exists.integration
                ):
                    continue
                endpoints = tuple(
                    sorted(
                        (
                            DiagnosticEndpoint(
                                application=endpoint.application,
                                endpoint=endpoint.endpoint,
                                charm_name=cls._application_charm_name(
                                    domain,
                                    endpoint.model if endpoint.model.name is not None else integration_exists.model,
                                    endpoint.application,
                                ),
                            )
                            for endpoint in integration_exists.integration
                        ),
                        key=lambda endpoint: endpoint.identity,
                    )
                )
                diagnostics.append(UnresolvedIntegrationDiagnostic(endpoints=endpoints))
        if not diagnostics:
            diagnostics.append(
                BundleBuildFailureDiagnostic(
                    kind=BundleBuildFailureKind.UNEXPANDABLE_ASSERTIONS,
                    detail="Cannot expand the domain to handle failed assertion tags",
                )
            )
        return canonicalize_diagnostics(diagnostics)

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
    ) -> AssertionHandlingResult:
        if tag.kind == Assertions.CHARM_ENDPOINT_NON_OPTIONAL:
            non_optional = cast(CharmEndpointNonOptionalTag, tag)
            return AssertionHandlingResult(
                expanded=self._expand_for_endpoint(
                    non_optional.charm.charm_id,
                    non_optional.charm.endpoint,
                    domain,
                )
            )

        elif tag.kind == Assertions.APPLICATION_EXISTS:
            app_exists = cast(ApplicationExistsTag, tag)
            model_ref = app_exists.model
            try:
                charm = self._get_charm_for_application(app_exists.application, domain, model_ref)
            except CharmReleaseNotFoundException as error:
                return AssertionHandlingResult(
                    diagnostics=(
                        self._application_release_diagnostic(
                            app_exists.application,
                            model_ref,
                            domain,
                            error,
                        ),
                    )
                )
            return AssertionHandlingResult(
                expanded=self._add_charm_for_application(
                    charm,
                    app_exists.application,
                    domain,
                    model_ref,
                )
            )

        elif tag.kind == Assertions.APPLICATION_INTEGRATION_EXISTS:
            app_integration_exists = cast(ApplicationIntegrationExistsTag, tag)
            results = []
            diagnostics = []
            for endpoint in app_integration_exists.integration:
                model_ref = endpoint.model if endpoint.model.name is not None else app_integration_exists.model
                try:
                    charm = self._get_charm_for_application(endpoint.application, domain, model_ref)
                except CharmReleaseNotFoundException as error:
                    diagnostics.append(
                        self._application_release_diagnostic(
                            endpoint.application,
                            model_ref,
                            domain,
                            error,
                        )
                    )
                    continue
                results.append(self._add_charm_for_application(charm, endpoint.application, domain, model_ref))
            if any(results):
                return AssertionHandlingResult(expanded=True, diagnostics=tuple(diagnostics))
            if diagnostics:
                return AssertionHandlingResult(diagnostics=tuple(diagnostics))
            # Both charms already exist but aren't paired (add_charm_to_domain no longer
            # eagerly pairs); connect them directly.
            return AssertionHandlingResult(expanded=self._connect_apps_for_integration(app_integration_exists, domain))

        elif tag.kind == Assertions.ENDPOINT_COUNT_MATCHES_INTEGRATIONS:
            count_tag = cast(EndpointCountMatchesIntegrationsTag, tag)
            return AssertionHandlingResult(
                expanded=self._expand_for_endpoint(
                    count_tag.charm.charm_id,
                    count_tag.charm.endpoint,
                    domain,
                )
            )

        elif tag.kind == Assertions.PEER_CHANNEL_MISMATCH:
            mismatch = cast(PeerChannelMismatchTag, tag)
            return AssertionHandlingResult(expanded=self._handle_peer_channel_mismatch(mismatch, domain))

        elif tag.kind == Assertions.SUBORDINATE_BASE_MISMATCH:
            base_mismatch = cast(SubordinateBaseMismatchTag, tag)
            return AssertionHandlingResult(expanded=self._handle_subordinate_base_mismatch(base_mismatch, domain))

        return AssertionHandlingResult()

    @classmethod
    def _application_release_diagnostic(
        cls,
        application: str,
        model_ref: ModelRef,
        domain: Domain,
        error: CharmReleaseNotFoundException,
    ) -> ApplicationReleaseDiagnostic:
        return ApplicationReleaseDiagnostic(
            application=application,
            charm_name=cls._application_charm_name(domain, model_ref, application),
            model=model_ref.key,
            error=error,
        )

    def _expand_for_endpoint(
        self,
        charm_id: int,
        endpoint_name: str,
        domain: Domain,
    ) -> bool:
        """Expand the domain to satisfy an unfulfilled endpoint.

        The owning model is tried before other models (which are skipped entirely for
        container-scoped endpoints). Existing compatible charms are reused first.
        Application charms expose every direct alternative; transitive dependencies add
        only the first viable candidate to keep the CEGIS domain small.
        """
        owning_model = domain.charms[charm_id].model
        endpoint = domain.charms[charm_id].spec.endpoints[endpoint_name]
        is_container_scoped = endpoint.scope == EndpointScope.CONTAINER
        models = (
            [owning_model] if is_container_scoped else [owning_model, *(m for m in domain.models if m != owning_model)]
        )

        # Exhaust the owning model before considering any other model, so a cheap local
        # integration is always preferred over a cross-model one.
        for model_ref in models:
            if self._connect_existing_for_endpoint(charm_id, endpoint_name, domain, model_ref):
                return True
            names = self._get_charms_for_endpoint(charm_id, endpoint_name, domain, model_ref)
            if not names:
                self.logger.debug(
                    f"No charms found for endpoint {domain.charms[charm_id].spec.name}:{endpoint_name} "
                    f"in model '{model_ref.key}'"
                )
            if self._add_available_charms(
                names,
                charm_id,
                endpoint_name,
                domain,
                model_ref,
                stop_after_first=not self._is_application_charm(charm_id, domain),
            ):
                return True

        return False

    def _add_available_charms(
        self,
        candidate_names: list[str],
        charm_id: int,
        endpoint_name: str,
        domain: Domain,
        model_ref: ModelRef,
        *,
        stop_after_first: bool = False,
    ) -> bool:
        """Fetch and add every compatible candidate for one failed endpoint.

        Each candidate is paired only with the parent charm. This gives the optimizer
        the same direct alternatives as eager expansion without pairing each new charm
        against the entire domain. Transitive dependency expansion sets
        ``stop_after_first`` to keep the CEGIS domain small.
        """
        model = domain.models[model_ref]
        endpoint = domain.charms[charm_id].spec.endpoints[endpoint_name]
        ubuntu_version: str | None = (
            domain.charms[charm_id].spec.ubuntu_version if endpoint.scope == EndpointScope.CONTAINER else None
        )
        added = False

        for charm_name in candidate_names:
            try:
                charm = self.charmhub_client.charm_from_store(
                    charm_name=charm_name,
                    ubuntu_arch=model.arch,
                    juju_version=model.juju_version,
                    platform=model.platform,
                    ubuntu_version=ubuntu_version,
                )
            except CharmReleaseNotFoundException:
                self.logger.debug(f"Skipping {charm_name}: no compatible release for {model.platform}/{model.arch}")
                continue
            if not self._has_compatible_endpoint(endpoint, charm.endpoints.values()):
                self.logger.debug(f"Skipping {charm_name}: no endpoint compatible with {endpoint_name}")
                continue
            if self._add_charm_for_charm_id(charm, charm_id, domain, model_ref):
                added = True
                if stop_after_first:
                    return True
        return added

    def _connect_existing_for_endpoint(
        self,
        charm_id: int,
        endpoint_name: str,
        domain: Domain,
        target_model: ModelRef,
    ) -> bool:
        """Pair charm_id with any existing domain charm that can satisfy endpoint_name.

        Only creates integration variables — never adds new charms. Only report success
        once the integration variable for this specific endpoint actually exists.
        """
        endpoint = domain.charms[charm_id].spec.endpoints[endpoint_name]
        for other_id, other_charm in enumerate(domain.charms):
            if other_id == charm_id:
                continue
            if other_charm.model != target_model:
                continue
            # Check this other charm has a compatible endpoint.
            if not self._has_compatible_endpoint(endpoint, other_charm.spec.endpoints.values()):
                continue
            if self._is_endpoint_connected_to(charm_id, endpoint_name, other_id, domain):
                continue

            pair_charms_in_domain(domain, charm_id, other_id)
            if self._is_endpoint_connected_to(charm_id, endpoint_name, other_id, domain):
                self.logger.debug(
                    f"Connected existing charm {other_charm.spec.name}:{other_id} "
                    f"to {domain.charms[charm_id].spec.name}:{charm_id} via {endpoint_name}"
                )
                return True
        return False

    def _prepare_optimization_domain(self, domain: Domain, model: z3.ModelRef) -> None:
        """Expose bounded alternatives that can improve the satisfiable graph."""
        active_charm_ids = [
            charm_id
            for charm_id, charm in enumerate(domain.charms)
            if z3.is_true(model.eval(charm.exists, model_completion=True))
        ]

        # CEGIS creates only the integrations needed for feasibility. Pairing the
        # already-small active graph lets optimization share an active provider across
        # multiple consumers without expanding any dependency frontier.
        for index, charm_id in enumerate(active_charm_ids):
            for other_id in active_charm_ids[index + 1 :]:
                pair_charms_in_domain(domain, charm_id, other_id)

        # Base/channel mismatch handlers may have discovered an inactive replacement
        # for an active charm. Connect only those same-charm variants to the active graph
        # so optimization can replace the whole charm, not just the mismatched endpoint.
        for charm_id in active_charm_ids:
            charm = domain.charms[charm_id]
            replacement_ids = [
                added_id
                for added_id in charm.charms_added
                if domain.charms[added_id].model == charm.model and domain.charms[added_id].spec.name == charm.spec.name
            ]
            for replacement_id in replacement_ids:
                for other_id in active_charm_ids:
                    if other_id != charm_id:
                        pair_charms_in_domain(domain, replacement_id, other_id)

        self._add_optimization_duplicates(domain, model)

    @staticmethod
    def _add_optimization_duplicates(domain: Domain, model: z3.ModelRef) -> None:
        """Expose cheaper duplicate-charm solutions before optimizing.

        Lazy expansion stops once the domain is satisfiable, but a smaller solution may
        require a second instance of an active charm to break a bidirectional-interface
        cycle or satisfy multiple required endpoints on one counterpart. Add one such
        candidate and pair it only with the active domain, avoiding the eager all-candidate
        pairing that caused the original timeout.
        """
        active_charm_ids = [
            charm_id
            for charm_id, charm in enumerate(domain.charms)
            if z3.is_true(model.eval(charm.exists, model_completion=True))
        ]
        active_interfaces: dict[int, set[str]] = {charm_id: set() for charm_id in active_charm_ids}
        for integration in domain.charm_integrations:
            if not z3.is_true(model.eval(integration.exists, model_completion=True)):
                continue
            interface = domain.integration_interface(integration)
            active_interfaces.get(integration.requires_charm_id, set()).add(interface)
            active_interfaces.get(integration.provides_charm_id, set()).add(interface)

        duplicate_ids: list[int] = []
        processed: list[int] = []
        for charm_id in active_charm_ids:
            charm = domain.charms[charm_id]
            if any(
                domain.charms[other_id].model == charm.model and domain.charms[other_id].spec == charm.spec
                for other_id in processed
            ):
                continue
            processed.append(charm_id)

            requires = {
                endpoint.interface
                for endpoint in charm.spec.endpoints.values()
                if endpoint.type == EndpointType.REQUIRES
            }
            provides = {
                endpoint.interface
                for endpoint in charm.spec.endpoints.values()
                if endpoint.type == EndpointType.PROVIDES
            }
            breaks_cycle = bool(active_interfaces[charm_id].intersection(requires, provides))
            serves_parallel_endpoints = any(
                BundleBuilder._can_serve_multiple_required_endpoints(charm.spec, other.spec)
                for other_id, other in enumerate(domain.charms)
                if other_id in active_charm_ids and other_id != charm_id and other.model == charm.model
            )
            if not breaks_cycle and not serves_parallel_endpoints:
                continue

            equivalent_ids = [
                other_id
                for other_id, other in enumerate(domain.charms)
                if other.model == charm.model and other.spec == charm.spec
            ]
            active_equivalent_ids = [
                equivalent_id for equivalent_id in equivalent_ids if equivalent_id in active_charm_ids
            ]
            for equivalent_id in active_equivalent_ids:
                for other_id in active_charm_ids:
                    if other_id != equivalent_id:
                        pair_charms_in_domain(domain, equivalent_id, other_id)

            if len(active_equivalent_ids) > 1:
                continue

            duplicate_id = next(
                (other_id for other_id in equivalent_ids if other_id not in active_charm_ids),
                None,
            )
            if duplicate_id is None:
                duplicate_id = add_charm_to_domain(charm.spec, domain, charm.model)

            for other_id in [*active_charm_ids, *duplicate_ids]:
                if other_id != duplicate_id:
                    pair_charms_in_domain(domain, duplicate_id, other_id)
            duplicate_ids.append(duplicate_id)

    @staticmethod
    def _connect_apps_for_integration(
        app_integration_exists: ApplicationIntegrationExistsTag,
        domain: Domain,
    ) -> bool:
        """Pair the charms currently mapped to each side of a user-specified app integration.

        add_charm_to_domain doesn't eagerly pair a new charm against the rest of the domain, so
        two applications can each have their charm added (via _add_charm_for_application, one
        no-op call per side) without ever being connected to each other. This handles that case
        directly: for every candidate charm currently mapped to each application (usually one
        each, but can be more if channel-mismatch resolution added variants), try pairing them.
        """
        endpoints = app_integration_exists.integration
        if len(endpoints) != 2:
            return False
        ep_a, ep_b = endpoints
        model_a = ep_a.model if ep_a.model.name is not None else app_integration_exists.model
        model_b = ep_b.model if ep_b.model.name is not None else app_integration_exists.model
        if model_a not in domain.models or model_b not in domain.models:
            return False  # external CMR endpoint - nothing in-domain to pair

        charm_ids_a = domain.models[model_a].applications[ep_a.application].charm_ids
        charm_ids_b = domain.models[model_b].applications[ep_b.application].charm_ids
        connected = False
        for charm_id_a in charm_ids_a:
            for charm_id_b in charm_ids_b:
                if charm_id_a == charm_id_b:
                    continue
                if pair_charms_in_domain(domain, charm_id_a, charm_id_b):
                    connected = True
        return connected

    @staticmethod
    def _has_compatible_endpoint(endpoint: CharmEndpoint, other_endpoints: Iterable[CharmEndpoint]) -> bool:
        """Check whether any of other_endpoints can semantically connect to endpoint.

        Two endpoints are compatible when they share the same interface and have opposite
        REQUIRES/PROVIDES directions.
        """
        return any(
            other_ep.interface == endpoint.interface
            and (
                (endpoint.type == EndpointType.REQUIRES and other_ep.type == EndpointType.PROVIDES)
                or (endpoint.type == EndpointType.PROVIDES and other_ep.type == EndpointType.REQUIRES)
            )
            for other_ep in other_endpoints
        )

    @staticmethod
    def _can_serve_multiple_required_endpoints(charm: Charm, counterpart: Charm) -> bool:
        """Return whether charm can satisfy more than one required endpoint on counterpart."""
        matching_required = sum(
            1
            for endpoint in counterpart.endpoints.values()
            if not endpoint.optional and BundleBuilder._has_compatible_endpoint(endpoint, charm.endpoints.values())
        )
        return matching_required > 1

    @staticmethod
    def _is_application_charm(charm_id: int, domain: Domain) -> bool:
        """Return whether charm_id is a candidate for a user-specified application."""
        return any(
            charm_id in application.charm_ids
            for domain_model in domain.models.values()
            for application in domain_model.applications.values()
        )

    @staticmethod
    def _is_endpoint_connected_to(charm_id: int, endpoint_name: str, other_id: int, domain: Domain) -> bool:
        """Check whether charm_id:endpoint_name already has an integration variable to other_id."""
        return any(
            (i.requires_charm_id, i.requires_endpoint, i.provides_charm_id) == (charm_id, endpoint_name, other_id)
            or (i.provides_charm_id, i.provides_endpoint, i.requires_charm_id) == (charm_id, endpoint_name, other_id)
            for i in domain.charm_integrations
        )

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
            expanded |= self._add_charm_for_charm_id(
                peer_charm,
                tag.peer_charm_id,
                domain,
                owning_model,
                connect_to_id=tag.charm.charm_id,
                connect_to_neighbors=True,
            )
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
            expanded |= self._add_charm_for_charm_id(
                owning_charm,
                tag.charm.charm_id,
                domain,
                owning_model,
                connect_to_id=tag.peer_charm_id,
                connect_to_neighbors=True,
            )
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
        interface = sub_charm_spec.endpoints[tag.subordinate_endpoint].interface

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
            expanded |= self._add_charm_for_charm_id(
                sub_charm,
                tag.subordinate_charm_id,
                domain,
                model_ref,
                connect_to_id=tag.principal_charm_id,
                connect_to_neighbors=True,
                connect_to_interface=interface,
            )
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
            expanded |= self._add_charm_for_charm_id(
                principal_charm,
                tag.principal_charm_id,
                domain,
                model_ref,
                connect_to_id=tag.subordinate_charm_id,
                connect_to_neighbors=True,
                connect_to_interface=interface,
            )
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
    ) -> list[str]:
        """Find candidate charm names that can fulfill an endpoint, sorted by priority.

        Returns names only (no network fetches beyond find_charms). Callers fetch either
        the first viable candidate for feasibility or every candidate for optimization.
        """
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

        # Sort by priority, then name — `fulfilling_charms` is a set with per-process
        # randomized order, so a deterministic tiebreak is needed for repeatable builds.
        return sorted(
            fulfilling_charms,
            key=lambda name: (-self.charmhub_client.overrides_client.get_charm_priority(name), name),
        )

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
        *,
        connect_to_id: int | None = None,
        connect_to_neighbors: bool = False,
        connect_to_interface: str | None = None,
    ) -> bool:
        parent_charm = domain.charms[charm_id]

        # Dedup per parent charm_id: one candidate instance is enough during expansion.
        # Additional instances needed only for optimization are added after satisfiability.
        if any(domain.charms[added_id].spec == charm for added_id in parent_charm.charms_added):
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

        # Pair direct dependencies with their parent. Replacement variants inherit
        # same-interface neighbors because one replacement may satisfy several counterparts.
        if connect_to_neighbors:
            counterpart_ids = {
                integration.provides_charm_id
                if integration.requires_charm_id == charm_id
                else integration.requires_charm_id
                for integration in domain.charm_integrations
                if charm_id in (integration.requires_charm_id, integration.provides_charm_id)
                and (connect_to_interface is None or domain.integration_interface(integration) == connect_to_interface)
            }
            if connect_to_id is not None:
                counterpart_ids.add(connect_to_id)
            for other_id in counterpart_ids:
                pair_charms_in_domain(domain, other_id, new_charm_id)
        else:
            pair_charms_in_domain(domain, connect_to_id if connect_to_id is not None else charm_id, new_charm_id)

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
            raise UncompletableBundleError(
                diagnostics=(
                    BundleBuildFailureDiagnostic(
                        kind=BundleBuildFailureKind.OPTIMIZATION_UNSATISFIABLE,
                        detail="Optimization failed because the problem became unsatisfiable",
                    ),
                )
            )
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
        solver.set("random_seed", _SOLVER_RANDOM_SEED)
        add_constraints(solver, domain)
        for c in extra_constraints or []:
            solver.add(c)

        # Seed from the caller's model to skip a redundant SAT solve.
        if initial_model is not None:
            model = initial_model
        else:
            result = solver.check()
            if result == z3.unsat:
                raise UncompletableBundleError(
                    diagnostics=(
                        BundleBuildFailureDiagnostic(
                            kind=BundleBuildFailureKind.OPTIMIZATION_UNSATISFIABLE,
                            detail="Optimization failed because the problem became unsatisfiable",
                        ),
                    )
                )
            if result != z3.sat:
                raise UncompletableBundleError(
                    diagnostics=(
                        BundleBuildFailureDiagnostic(
                            kind=BundleBuildFailureKind.OPTIMIZATION_TIMEOUT,
                            detail="Optimization failed because the initial solve timed out",
                        ),
                    )
                )
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
