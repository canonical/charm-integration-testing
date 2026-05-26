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
from .charm import Charm, CharmChannel, EndpointScope, EndpointType
from .charmhub import CharmhubClient
from .charmhub_http import CharmReleaseNotFoundException
from .constraints import add_constraints
from .domain import (
    Domain,
    ModelRef,
    add_charm_to_domain,
)
from .domain_builder import DomainBuilder
from .extract import extract_solution
from .snapstore import SnapstoreClient
from .spec import SpecFile
from .timing import NullTimeline, Timeline

_DEFAULT_OPTIMIZE_TIMEOUT = timedelta(minutes=3)

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
        # Iterative loop: expand domain until satisfiable
        max_iterations = 100
        for iteration in range(max_iterations):
            self.logger.info(f"Iteration {iteration + 1}/{max_iterations}")

            # Create solver with unsat core tracking
            solver = z3.Solver()
            solver.set("unsat_core", True)

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
                try:
                    self.logger.info("Re-solving with optimization to minimize applications and integrations")
                    t_opt = self.timeline.on(f"iter{iteration}/optimize")
                    model = self._optimize_solution(domain)
                    self.timeline.off(t_opt)
                except TimeoutError:
                    self.logger.warning("Optimization timed out; using unoptimized solution")
                return model

            elif result == z3.unsat:
                self.logger.info("Problem is unsatisfiable; expanding domain")

                unsat_core = solver.unsat_core()
                if len(unsat_core) == 0:
                    self.logger.warning("Solver returned unsat but unsat core was empty")
                    raise UncompletableBundleError("Solver returned unsat but unsat core was empty")

                decoded_core: list[AssertionTag] = sorted(
                    [AssertionTag.decode(str(assertion)) for assertion in unsat_core],
                    key=lambda a: (_EXPANSION_PRIORITY.get(a.kind, len(_EXPANSION_PRIORITY)), str(a)),
                )

                domain_modified = False
                for tag in decoded_core:
                    self.logger.debug(f"Unsat core item: {tag}")
                    domain_modified = self._handle_failed_assertion(tag, domain)
                    if domain_modified:
                        self.logger.info(f"Expanded domain to handle failed assertion tag: {tag}")
                        break

                if not domain_modified:
                    raise UncompletableBundleError(unsat_core=decoded_core)
            else:
                raise UncompletableBundleError("Solver returned unknown")

        raise UncompletableBundleError(f"Could not satisfy constraints after {max_iterations} iterations")

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

        Tries the owning model first. If no same-platform charm can satisfy the
        endpoint (e.g. a kubernetes charm requiring an interface that only a
        machine charm provides), tries other models. Adding a provider to
        another model creates a cross-model DomainCharmIntegration that the solver can activate on
        the next iteration.
        """
        owning_model = domain.charms[charm_id].model

        charms = self._get_charms_for_endpoint(charm_id, endpoint_name, domain, owning_model)
        if not charms:
            self.logger.debug(
                f"No charms found for endpoint {domain.charms[charm_id].spec.name}:{endpoint_name} "
                f"in model '{owning_model.key}'"
            )
        results = [self._add_charm_for_charm_id(charm, charm_id, domain, owning_model) for charm in charms]

        if not any(results):
            for other_model_ref in domain.models:
                if other_model_ref == owning_model:
                    continue
                other_charms = self._get_charms_for_endpoint(charm_id, endpoint_name, domain, other_model_ref)
                other_results = [
                    self._add_charm_for_charm_id(charm, charm_id, domain, other_model_ref) for charm in other_charms
                ]
                if any(other_results):
                    return True
        return any(results)

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

    def _optimize_solution(self, domain: Domain) -> z3.ModelRef:
        scale = 1_000_000
        epsilon = 1e-6

        charm_cost_expr = z3.Sum(
            [
                z3.If(
                    charm.exists,
                    z3.IntVal(int(scale / max(charm.spec.priority, epsilon))),
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

        timeout_ms = int(self.optimize_timeout.total_seconds() * 1000)

        opt = z3.Optimize()
        opt.set("timeout", timeout_ms)
        add_constraints(opt, domain)
        opt.minimize(charm_cost_expr)
        opt.minimize(integration_cost_expr)
        result = opt.check()
        if result == z3.sat:
            self.logger.info("z3.Optimize found optimal solution")
            return opt.model()
        if result == z3.unsat:
            raise UncompletableBundleError("Optimization failed - problem became unsatisfiable")
        raise TimeoutError("Optimization timed out")
