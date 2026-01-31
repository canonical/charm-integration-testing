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

from bundle_builder_v3.charm import EndpointType

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
                for assertion in unsat_core:
                    self.logger.debug(f"Unsat core item: {assertion}")
                domain = self._handle_failed_assertions([str(assertion) for assertion in unsat_core], domain)
            else:
                raise UnresolvableBundleError("Solver returned unknown")

        raise UnresolvableBundleError(f"Could not satisfy constraints after {max_iterations} iterations")

    def _handle_failed_assertions(
        self,
        failed_assertions: list[str],
        domain: Domain,
    ) -> Domain:
        if not failed_assertions:
            self.logger.warning("Solver returned unsat but unsat core was empty")
            raise UnresolvableBundleError("Solver returned unsat but unsat core was empty")

        # Handle an assertions that have not been previously handled
        charms_to_add: list[Charm] = []
        for assertion in failed_assertions:
            if assertion in domain.handled_failed_assertions:
                continue

            try:
                tag = AssertionTag.decode(assertion)
            except ValueError:
                continue

            domain.handled_failed_assertions.add(assertion)

            if tag.kind == Assertions.CHARM_ENDPOINT_NON_OPTIONAL:
                non_optional = cast(CharmEndpointNonOptionalTag, tag)
                for charm in self._get_charms_for_endpoint(
                    non_optional.charm.charm_id, non_optional.charm.endpoint, domain
                ):
                    if charm not in charms_to_add:
                        charms_to_add.append(charm)
            if tag.kind == Assertions.APPLICATION_EXISTS:
                app_exists = cast(ApplicationExistsTag, tag)
                charm = self._get_charm_for_application(app_exists.application, domain)
                if charm not in charms_to_add:
                    charms_to_add.append(charm)
            if tag.kind == Assertions.APPLICATION_INTEGRATION_EXISTS:
                app_integration_exists = cast(ApplicationIntegrationExistsTag, tag)
                for endpoint in app_integration_exists.integration:
                    app_exists_assertion = ApplicationExistsTag(application=endpoint.application).encode()
                    if app_exists_assertion in domain.handled_failed_assertions:
                        continue
                    domain.handled_failed_assertions.add(app_exists_assertion)
                    charm = self._get_charm_for_application(endpoint.application, domain)
                    if charm not in charms_to_add:
                        charms_to_add.append(charm)
            if tag.kind == Assertions.ENDPOINT_COUNT_MATCHES_INTEGRATIONS:
                endpoint_count_matches_integrations = cast(EndpointCountMatchesIntegrationsTag, tag)
                non_optional_assertion = CharmEndpointNonOptionalTag(
                    charm=endpoint_count_matches_integrations.charm
                ).encode()
                if non_optional_assertion in domain.handled_failed_assertions:
                    continue
                domain.handled_failed_assertions.add(non_optional_assertion)
                for charm in self._get_charms_for_endpoint(
                    endpoint_count_matches_integrations.charm.charm_id,
                    endpoint_count_matches_integrations.charm.endpoint,
                    domain,
                ):
                    if charm not in charms_to_add:
                        charms_to_add.append(charm)

        if len(charms_to_add) == 0:
            raise UnresolvableBundleError(f"Cannot handle failed assertions: {failed_assertions}")

        for charm in charms_to_add:
            self.logger.info(f"Adding charm '{charm.name}' to domain to resolve failed assertions")
            domain = add_charm_to_domain(charm, domain)

        return domain

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
