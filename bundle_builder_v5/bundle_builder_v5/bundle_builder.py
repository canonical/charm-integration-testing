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

import z3

from bundle_builder_v5.charm import EndpointType

from .assertion_tags import (
    ApplicationExistsTag,
    ApplicationIntegrationExistsTag,
    Assertions,
    AssertionTag,
    CharmEndpointNonOptionalTag,
    EndpointCountMatchesIntegrationsTag,
)
from .bundle import Bundle
from .charm import Charm
from .charmhub import CharmhubClient
from .constraints import add_constraints
from .domain import (
    ApplicationConstraint,
    Domain,
    IntegrationConstraint,
    add_charm_to_domain,
    initialize_domain,
)
from .extract import extract_bundle


class UnresolvableBundleError(Exception):
    def __init__(self, message: str):
        super().__init__(message)


class BundleBuilder:
    charmhub_client: CharmhubClient
    logger: logging.Logger

    def __init__(
        self,
        charmhub_client: CharmhubClient,
        logger: logging.Logger = logging.getLogger(__name__),
    ):
        self.charmhub_client = charmhub_client
        self.logger = logger

    def build(
        self,
        applications: dict[str, ApplicationConstraint],
        integrations: set[IntegrationConstraint],
        platform: str,
        arch: str,
    ) -> Bundle:
        # Initialize domain with user constraints
        domain = initialize_domain(applications=applications, integrations=integrations, platform=platform, arch=arch)

        # Iterative loop: expand domain until satisfiable
        max_iterations = 100
        for iteration in range(max_iterations):
            self.logger.info(f"Iteration {iteration + 1}/{max_iterations}")

            # Create solver with unsat core tracking
            solver = z3.Solver()
            solver.set("unsat_core", True)

            # Add constraints
            add_constraints(solver, domain)

            # Check satisfiability
            result = solver.check()

            if result == z3.sat:
                self.logger.info("Problem is satisfiable")
                model = solver.model()
                try:
                    self.logger.info("Re-solving with optimization to minimize charms and integrations")
                    model = self._optimize_solution(domain)
                except TimeoutError:
                    self.logger.warning("Optimization timed out; using unoptimized solution")
                return extract_bundle(model, domain, logger=self.logger)

            elif result == z3.unsat:
                self.logger.info("Problem is unsatisfiable; expanding domain")

                unsat_core = solver.unsat_core()
                if len(unsat_core) == 0:
                    self.logger.warning("Solver returned unsat but unsat core was empty")
                    raise UnresolvableBundleError("Solver returned unsat but unsat core was empty")

                domain_modified = False
                for assertion in unsat_core:
                    self.logger.debug(f"Unsat core item: {assertion}")
                    domain_modified = domain_modified or self._handle_failed_assertion(str(assertion), domain)

                if not domain_modified:
                    raise UnresolvableBundleError("Cannot expand domain to handle failed assertions")
            else:
                raise UnresolvableBundleError("Solver returned unknown")

        raise UnresolvableBundleError(f"Could not satisfy constraints after {max_iterations} iterations")

    def _handle_failed_assertion(
        self,
        assertion: str,
        domain: Domain,
    ) -> bool:
        tag = AssertionTag.decode(assertion)

        if tag.kind == Assertions.CHARM_ENDPOINT_NON_OPTIONAL:
            non_optional = cast(CharmEndpointNonOptionalTag, tag)
            charms = self._get_charms_for_endpoint(non_optional.charm.charm_id, non_optional.charm.endpoint, domain)
            results = [self._add_charm_for_charm_id(charm, non_optional.charm.charm_id, domain) for charm in charms]
            return any(results)

        elif tag.kind == Assertions.APPLICATION_EXISTS:
            app_exists = cast(ApplicationExistsTag, tag)
            charm = self._get_charm_for_application(app_exists.application, domain)
            return self._add_charm_for_application(charm, app_exists.application, domain)

        elif tag.kind == Assertions.APPLICATION_INTEGRATION_EXISTS:
            app_integration_exists = cast(ApplicationIntegrationExistsTag, tag)
            results = [
                self._add_charm_for_application(
                    self._get_charm_for_application(endpoint.application, domain), endpoint.application, domain
                )
                for endpoint in app_integration_exists.integration
            ]
            return any(results)

        elif tag.kind == Assertions.ENDPOINT_COUNT_MATCHES_INTEGRATIONS:
            endpoint_count_matches_integrations = cast(EndpointCountMatchesIntegrationsTag, tag)
            charms = self._get_charms_for_endpoint(
                endpoint_count_matches_integrations.charm.charm_id,
                endpoint_count_matches_integrations.charm.endpoint,
                domain,
            )
            results = [
                self._add_charm_for_charm_id(charm, endpoint_count_matches_integrations.charm.charm_id, domain)
                for charm in charms
            ]
            return any(results)

        return False

    def _get_charm_for_application(self, application: str, domain: Domain) -> Charm:
        # Get the charm matching the application constraints
        constraints = domain.application_constraints[application]
        return self.charmhub_client.charm_from_store(
            charm_name=constraints.charm,
            ubuntu_arch=domain.arch_constraint,
            charm_channel=constraints.channel,
            charm_revision=constraints.revision,
            ubuntu_version=constraints.base,
        )

    def _get_charms_for_endpoint(self, charm_id: int, endpoint_name: str, domain: Domain) -> list[Charm]:
        endpoint = domain.charms[charm_id].spec.endpoints[endpoint_name]
        # requesting_charm_name = domain.charms[charm_id].spec.name

        fulfilling_charms: set[str] = set()
        if endpoint.type == EndpointType.REQUIRES:
            fulfilling_charms = self.charmhub_client.find_charms(
                provides=endpoint.interface, platform=domain.platform_constraint
            )
        elif endpoint.type == EndpointType.PROVIDES:
            fulfilling_charms = self.charmhub_client.find_charms(
                requires=endpoint.interface, platform=domain.platform_constraint
            )

        return [
            self.charmhub_client.charm_from_store(
                charm_name=charm,
                ubuntu_arch=domain.arch_constraint,
            )
            for charm in fulfilling_charms
        ]

    def _add_charm_for_application(self, charm: Charm, application: str, domain: Domain) -> bool:
        # Check if this charm has already been added for this application
        if application in domain.charms_added_for_application:
            for charm_id in domain.charms_added_for_application[application]:
                if domain.charms[charm_id].spec == charm:
                    return False

        # Add charm to domain
        self.logger.debug(f"Adding charm {charm.name} to satisfy application {application}")
        charm_id = add_charm_to_domain(charm, domain)

        # Record that this charm was added for this application
        if application not in domain.charms_added_for_application:
            domain.charms_added_for_application[application] = []
        domain.charms_added_for_application[application].append(charm_id)
        return True

    def _add_charm_for_charm_id(self, charm: Charm, charm_id: int, domain: Domain) -> bool:
        # Check if this exact charm was already added for this charm_id
        if charm_id in domain.charms_added_for_charm:
            for added_charm_id in domain.charms_added_for_charm[charm_id]:
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
            for parent_id, children_ids in domain.charms_added_for_charm.items():
                if ancestor_id in children_ids:
                    stack.append(parent_id)

        # Add charm to domain
        self.logger.debug(f"Adding charm {charm.name} to satisfy charm {domain.charms[charm_id].spec.name}:{charm_id}")
        new_charm_id = add_charm_to_domain(charm, domain)

        # Record that this charm was added for this charm_id
        if charm_id not in domain.charms_added_for_charm:
            domain.charms_added_for_charm[charm_id] = []
        domain.charms_added_for_charm[charm_id].append(new_charm_id)
        return True

    def _optimize_solution(self, domain: Domain, timeout: timedelta = timedelta(minutes=3)) -> z3.ModelRef:
        optimizer = z3.Optimize()
        optimizer.set("timeout", int(timeout.total_seconds() * 1000))
        add_constraints(optimizer, domain)

        scale = 1_000_000
        epsilon = 1e-6
        optimizer.minimize(
            z3.Sum(
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
        )

        optimizer.minimize(
            z3.Sum(
                [z3.If(integration.exists, 1, 0) for integration in domain.charm_integrations.values()] + [z3.IntVal(0)]
            )
        )

        result = optimizer.check()
        if result == z3.unknown:
            raise TimeoutError("Optimization timed out")
        if result != z3.sat:
            raise UnresolvableBundleError("Optimization failed - problem became unsatisfiable")

        return optimizer.model()
