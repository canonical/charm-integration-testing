# Copyright (C) 2026 Canonical Ltd

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


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
from .charm import Charm, CharmChannel, CharmEndpoint, EndpointScope, EndpointType
from .charmhub import CharmhubClient
from .charmhub_http import CharmReleaseNotFoundException
from .constraints import add_constraints
from .domain import (
    Domain,
    ModelRef,
    add_charm_to_domain,
    get_or_create_integration,
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
    """Exception raised when bundle builder cannot generate a complete bundle from the base bundle"""

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
        domain = self._domain_builder.build(spec)
        z3_model = self._solve(domain)
        return extract_solution(z3_model, domain, logger=self.logger)

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
            return self._satisfy_endpoint(non_optional.charm.charm_id, non_optional.charm.endpoint, domain)

        elif tag.kind == Assertions.APPLICATION_EXISTS:
            app_exists = cast(ApplicationExistsTag, tag)
            model_ref = app_exists.model
            charm = self._get_charm_for_application(app_exists.application, domain, model_ref)
            return self._add_charm_for_application(charm, app_exists.application, domain, model_ref)

        elif tag.kind == Assertions.APPLICATION_INTEGRATION_EXISTS:
            app_integration_exists = cast(ApplicationIntegrationExistsTag, tag)
            return self._satisfy_application_integration(app_integration_exists, domain)

        elif tag.kind == Assertions.ENDPOINT_COUNT_MATCHES_INTEGRATIONS:
            count_tag = cast(EndpointCountMatchesIntegrationsTag, tag)
            return self._satisfy_endpoint(count_tag.charm.charm_id, count_tag.charm.endpoint, domain)

        elif tag.kind == Assertions.PEER_CHANNEL_MISMATCH:
            mismatch = cast(PeerChannelMismatchTag, tag)
            return self._handle_peer_channel_mismatch(mismatch, domain)

        elif tag.kind == Assertions.SUBORDINATE_BASE_MISMATCH:
            base_mismatch = cast(SubordinateBaseMismatchTag, tag)
            return self._handle_subordinate_base_mismatch(base_mismatch, domain)

        return False

    def _satisfy_endpoint(
        self,
        charm_id: int,
        endpoint_name: str,
        domain: Domain,
    ) -> bool:
        """Create exactly ONE integration variable to help satisfy charm_id:endpoint_name.

        Capacity-aware CEGIS on integrations:
          1. REUSE - connect to an existing in-domain charm whose compatible
             endpoint has spare capacity. Creates one integration variable; no
             new charm.
          2. INSTANTIATE - if no reusable partner exists, fetch a new charm from
             Charmhub, add it, and create one integration variable.

        Reuse-before-instantiate keeps the integration-variable count O(endpoint
        demand) instead of O(providers x requirers):
          - unlimited provider: every requirer reuses the single instance.
          - limit:N provider: the instance saturates after N integrations, so the
            next requirer instantiates a fresh one -> ceil(demand / N) instances.

        The reuse pass already handles deduplication via integration_index (skipping
        providers already wired to this requirer) and saturation via _is_saturated.
        This means the reuse pass naturally finds the first available unsaturated
        partner from any chain, including those added by independent requirer chains.

        Tries the owning model first, then other models (cross-model relations).
        Returns True if a variable (and possibly a charm) was added.
        """
        owning_model = domain.charms[charm_id].model
        models_in_order = [owning_model] + [m for m in domain.models if m != owning_model]

        # Pass 1: reuse an existing in-domain partner that has spare capacity.
        # _best_reuse_partner skips providers already wired to charm_id (via
        # integration_index) and saturated providers (via _is_saturated), so
        # this pass never creates duplicate variables.
        for model_ref in models_in_order:
            pair = self._best_reuse_partner(charm_id, endpoint_name, domain, model_ref)
            if pair is not None:
                req_cid, req_ep, prov_cid, prov_ep = pair
                get_or_create_integration(domain, req_cid, req_ep, prov_cid, prov_ep)
                self.logger.debug(
                    f"Reusing in-domain partner for {domain.charms[charm_id].spec.name}:{endpoint_name} "
                    f"-> charm id {prov_cid if prov_cid != charm_id else req_cid}"
                )
                return True

        # Pass 2: instantiate a new partner charm from Charmhub and wire to it.
        for model_ref in models_in_order:
            charms = self._get_charms_for_endpoint(charm_id, endpoint_name, domain, model_ref)
            for partner in sorted(charms, key=lambda c: c.priority, reverse=True):
                new_id = self._add_charm_for_charm_id(partner, charm_id, domain, model_ref)
                if new_id is None:
                    continue
                pair = self._semantic_pair_with_charm(charm_id, endpoint_name, new_id, domain)
                if pair is None:
                    continue  # partner matched the interface query but had no compatible endpoint
                req_cid, req_ep, prov_cid, prov_ep = pair
                get_or_create_integration(domain, req_cid, req_ep, prov_cid, prov_ep)
                return True

        self.logger.debug(f"No partner found for endpoint {domain.charms[charm_id].spec.name}:{endpoint_name}")
        return False

    def _endpoint_var_count(self, domain: Domain, cid: int, endpoint_name: str) -> int:
        """Number of integration variables already incident on (cid, endpoint_name)."""
        count = 0
        for integration in domain.charm_integrations:
            if (integration.requires_charm_id == cid and integration.requires_endpoint == endpoint_name) or (
                integration.provides_charm_id == cid and integration.provides_endpoint == endpoint_name
            ):
                count += 1
        return count

    def _is_saturated(self, domain: Domain, cid: int, endpoint_name: str) -> bool:
        """True when (cid, endpoint_name) already has >= limit integration variables.

        An endpoint with an explicit limit can be ON in at most `limit` integrations,
        so offering a (limit+1)th variable cannot help: if the solver wanted to use
        this endpoint for the new consumer it would have to drop an existing
        consumer, which then re-triggers expansion and obtains its own partner. So
        skipping a saturated endpoint and instantiating a fresh partner instead is
        both complete and avoids the O(consumers x providers) variable blow-up.
        """
        limit = domain.charms[cid].spec.endpoints[endpoint_name].limit
        return limit is not None and self._endpoint_var_count(domain, cid, endpoint_name) >= limit

    def _scope_compatible(
        self,
        my_ep: CharmEndpoint,
        other_ep: CharmEndpoint,
        same_model: bool,
        req_model: ModelRef,
        prov_model: ModelRef,
        domain: Domain,
    ) -> bool:
        """Container-scoped (subordinate) relations must be co-located on a machine model."""
        if my_ep.scope == EndpointScope.CONTAINER or other_ep.scope == EndpointScope.CONTAINER:
            if not same_model:
                return False
            req_platform = domain.models[req_model].platform if req_model in domain.models else None
            prov_platform = domain.models[prov_model].platform if prov_model in domain.models else None
            if req_platform != "machine" or prov_platform != "machine":
                return False
        return True

    def _semantic_pair_with_charm(
        self,
        my_cid: int,
        my_endpoint: str,
        other_cid: int,
        domain: Domain,
    ) -> tuple[int, str, int, str] | None:
        """Find a compatible endpoint on other_cid and return the (req, prov) ordering.

        Returns (requires_charm_id, requires_endpoint, provides_charm_id, provides_endpoint)
        for the first compatible endpoint, or None if no compatible endpoint exists.
        """
        my_charm = domain.charms[my_cid]
        other_charm = domain.charms[other_cid]
        my_ep = my_charm.spec.endpoints[my_endpoint]
        same_model = my_charm.model == other_charm.model
        for other_endpoint, other_ep in other_charm.spec.endpoints.items():
            if other_ep.interface != my_ep.interface:
                continue
            if my_ep.type == EndpointType.REQUIRES and other_ep.type == EndpointType.PROVIDES:
                req_cid, req_ep, prov_cid, prov_ep = my_cid, my_endpoint, other_cid, other_endpoint
            elif my_ep.type == EndpointType.PROVIDES and other_ep.type == EndpointType.REQUIRES:
                req_cid, req_ep, prov_cid, prov_ep = other_cid, other_endpoint, my_cid, my_endpoint
            else:
                continue
            if not self._scope_compatible(
                my_ep, other_ep, same_model, domain.charms[req_cid].model, domain.charms[prov_cid].model, domain
            ):
                continue
            return req_cid, req_ep, prov_cid, prov_ep
        return None

    def _best_reuse_partner(
        self,
        charm_id: int,
        endpoint_name: str,
        domain: Domain,
        model_ref: ModelRef,
    ) -> tuple[int, str, int, str] | None:
        """Pick the highest-priority in-domain partner with spare capacity.

        Considers every existing charm in model_ref (excluding self) with a
        compatible endpoint that is not saturated and not already wired to
        (charm_id, endpoint_name). Returns the (req, prov) ordering for the best
        candidate, or None.
        """
        if self._is_saturated(domain, charm_id, endpoint_name):
            return None

        best: tuple[int, str, int, str] | None = None
        best_priority = float("-inf")
        for other_cid, other_charm in enumerate(domain.charms):
            if other_cid == charm_id or other_charm.model != model_ref:
                continue
            pair = self._semantic_pair_with_charm(charm_id, endpoint_name, other_cid, domain)
            if pair is None:
                continue
            req_cid, req_ep, prov_cid, prov_ep = pair
            if (req_cid, req_ep, prov_cid, prov_ep) in domain.integration_index:
                continue  # already offered this exact pair
            # The partner is whichever charm is not me.
            partner_cid, partner_ep = (prov_cid, prov_ep) if prov_cid != charm_id else (req_cid, req_ep)
            if self._is_saturated(domain, partner_cid, partner_ep):
                continue
            priority = other_charm.spec.priority
            if priority > best_priority:
                best_priority = priority
                best = pair
        return best

    def _semantic_pair_named(
        self,
        cid1: int,
        ep1: str,
        cid2: int,
        ep2: str,
        domain: Domain,
    ) -> tuple[int, str, int, str] | None:
        """Order two explicitly-named charm endpoints into (req, prov), or None.

        Used for user-specified integrations where the endpoints are named by the
        spec, so we honour them directly without a scope check.
        """
        e1 = domain.charms[cid1].spec.endpoints.get(ep1)
        e2 = domain.charms[cid2].spec.endpoints.get(ep2)
        if e1 is None or e2 is None:
            return None
        if e1.type == EndpointType.REQUIRES and e2.type == EndpointType.PROVIDES:
            return cid1, ep1, cid2, ep2
        if e1.type == EndpointType.PROVIDES and e2.type == EndpointType.REQUIRES:
            return cid2, ep2, cid1, ep1
        return None

    def _satisfy_application_integration(
        self,
        tag: ApplicationIntegrationExistsTag,
        domain: Domain,
    ) -> bool:
        """Satisfy a user-specified integration: ensure both charms, then wire them.

        Unlike endpoint demand (which discovers a partner), the spec names exactly
        which two application endpoints to integrate, so we create the integration
        variable(s) directly between the charms backing those applications.
        """
        endpoints = tag.integration
        if len(endpoints) != 2:
            return False

        expanded = False

        # 1. Ensure both applications have a backing charm.
        for ep in endpoints:
            model_ref = ep.model if ep.model.name is not None else tag.model
            charm = self._get_charm_for_application(ep.application, domain, model_ref)
            if self._add_charm_for_application(charm, ep.application, domain, model_ref):
                expanded = True

        # 2. Create the integration variable(s) between the backing charms.
        ep1, ep2 = endpoints[0], endpoints[1]
        m1 = ep1.model if ep1.model.name is not None else tag.model
        m2 = ep2.model if ep2.model.name is not None else tag.model
        app1 = domain.models[m1].applications.get(ep1.application) if m1 in domain.models else None
        app2 = domain.models[m2].applications.get(ep2.application) if m2 in domain.models else None
        if app1 is not None and app2 is not None:
            for cid1 in list(app1.charm_ids):
                for cid2 in list(app2.charm_ids):
                    if cid1 == cid2:
                        continue
                    pair = self._semantic_pair_named(cid1, ep1.endpoint, cid2, ep2.endpoint, domain)
                    if pair is None:
                        continue
                    before = len(domain.charm_integrations)
                    get_or_create_integration(domain, *pair)
                    if len(domain.charm_integrations) > before:
                        expanded = True
        return expanded

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
            expanded |= self._wire_variant(peer_charm, tag.peer_charm_id, tag.charm.charm_id, domain, owning_model)
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
            expanded |= self._wire_variant(owning_charm, tag.charm.charm_id, tag.peer_charm_id, domain, owning_model)
        except CharmReleaseNotFoundException:
            self.logger.debug(
                f"No release found for {tag.charm.charm_name} on {peer_channel.track}/{peer_channel.risk or '*'}"
            )

        return expanded

    def _wire_all_matching(self, cid_a: int, cid_b: int, domain: Domain) -> bool:
        """Create integration variables for every compatible endpoint pair between two charms.

        Used after fetching a charm variant (peer/subordinate mismatch handlers):
        the variant must be offered as an integration partner to the specific other
        charm so the solver can select the matching pair. Bounded to a single pair
        of charms, so no combinatorial growth.
        """
        created = False
        for ep_name in domain.charms[cid_a].spec.endpoints:
            pair = self._semantic_pair_with_charm(cid_a, ep_name, cid_b, domain)
            if pair is None:
                continue
            before = len(domain.charm_integrations)
            get_or_create_integration(domain, *pair)
            if len(domain.charm_integrations) > before:
                created = True
        return created

    def _wire_variant(
        self,
        variant_charm: Charm,
        variant_parent_id: int,
        wire_to_id: int,
        domain: Domain,
        model_ref: ModelRef,
    ) -> bool:
        """Add a charm variant (cycle-safe) and wire it to a specific other charm.

        Returns True if the variant charm was added (a new expansion step), even
        when no new integration var was needed.
        """
        new_id = self._add_charm_for_charm_id(variant_charm, variant_parent_id, domain, model_ref)
        if new_id is None:
            return False
        self._wire_all_matching(new_id, wire_to_id, domain)
        return True

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
            expanded |= self._wire_variant(
                sub_charm, tag.subordinate_charm_id, tag.principal_charm_id, domain, model_ref
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
            expanded |= self._wire_variant(
                principal_charm, tag.principal_charm_id, tag.subordinate_charm_id, domain, model_ref
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
    ) -> list[Charm]:
        """Fetch NEW candidate charms from Charmhub that can fulfill an endpoint.

        This is the "instantiate" path of _satisfy_endpoint. Reuse of charms
        already in the domain is handled separately and capacity-aware by
        _best_reuse_partner, so this method intentionally fetches fresh specs -
        including a fresh instance of a charm whose existing instances are all
        saturated (e.g. a second limit:1 provider for a second consumer).
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

        # On non-machine models, drop any candidate whose only matching endpoint is
        # container-scoped.  Container (subordinate) relations require machine co-location
        # and cannot be used on kubernetes or other non-machine platforms.  Filtering here
        # prevents _add_charm_for_charm_id from consuming a charms_added slot for a
        # partner that _semantic_pair_with_charm would immediately reject, which would
        # permanently block re-trying valid alternatives on the next CEGIS iteration.
        if model.platform != "machine":
            partner_type = EndpointType.PROVIDES if endpoint.type == EndpointType.REQUIRES else EndpointType.REQUIRES
            results = [
                c
                for c in results
                if any(
                    ep.interface == endpoint.interface
                    and ep.type == partner_type
                    and ep.scope != EndpointScope.CONTAINER
                    for ep in c.endpoints.values()
                )
            ]

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
    ) -> int | None:
        """Add `charm` as a dependency of `charm_id`; return the new charm id, or None.

        Returns None (no charm added) when the exact spec was already added for this
        parent, or when adding it would create a dependency cycle.
        """
        # Check if this exact charm was already added for this charm_id
        parent_charm = domain.charms[charm_id]
        for added_charm_id in parent_charm.charms_added:
            if domain.charms[added_charm_id].spec == charm:
                return None

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
                return None

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
        return new_charm_id

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
