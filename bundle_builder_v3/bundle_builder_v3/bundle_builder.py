# Copyright (C) 2025 Canonical Ltd

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
from typing import cast

import z3

from .assertion_tags import ApplicationExistsTag, Assertions, AssertionTag, CharmEndpointNonOptionalTag
from .bundle import Bundle, Integration
from .charmhub import CharmhubClient
from .constraints import add_constraints
from .extract import extract_bundle
from .problem_space import (
    ApplicationConstraint,
    ProblemSpace,
    add_charm_to_problem_space,
    add_charms_for_endpoint,
    initialize_problem_space,
)


class UnresolvableBundleError(Exception):
    def __init__(self, message: str, best_bundle: Bundle | None = None):
        super().__init__(message)
        self.best_bundle = best_bundle


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
        integrations: set[Integration],
        platform: str,
        arch: str,
    ) -> Bundle:
        # Initialize problem space with user constraints
        problem_space = initialize_problem_space(
            applications=applications, integrations=integrations, platform=platform, arch=arch
        )

        # Iterative loop: expand problem space until satisfiable
        max_iterations = 100
        for iteration in range(max_iterations):
            self.logger.info(f"Iteration {iteration + 1}/{max_iterations}")

            # Create solver with unsat core tracking
            solver = z3.Solver()
            solver.set("unsat_core", True)

            # Add constraints
            add_constraints(solver, problem_space)

            # Check satisfiability
            result = solver.check()

            if result == z3.sat:
                self.logger.info("Problem is satisfiable")
                self.logger.info("Re-solving with optimization to minimize charms and integrations")
                model = self._optimize_solution(problem_space)
                return extract_bundle(model, problem_space, logger=self.logger)

            elif result == z3.unsat:
                self.logger.info("Problem is unsatisfiable; expanding problem space")
                unsat_core = solver.unsat_core()
                for assertion in unsat_core:
                    self.logger.debug(f"Unsat core item: {assertion}")
                problem_space = self._handle_failed_assertions(
                    [str(assertion) for assertion in unsat_core], problem_space
                )
            else:
                raise UnresolvableBundleError("Solver returned unknown")

        raise UnresolvableBundleError(f"Could not satisfy constraints after {max_iterations} iterations")

    def _handle_failed_assertions(
        self,
        failed_assertions: list[str],
        problem_space: ProblemSpace,
    ) -> ProblemSpace:
        if not failed_assertions:
            self.logger.warning("Solver returned unsat but unsat core was empty")
            raise UnresolvableBundleError("Solver returned unsat but unsat core was empty")

        # Handle an assertions that have not been previously handled
        for assertion in failed_assertions:
            if assertion in problem_space.handled_failed_assertions:
                continue
            try:
                tag = AssertionTag.decode(assertion)
            except ValueError:
                continue

            if tag.kind == Assertions.CHARM_ENDPOINT_NON_OPTIONAL:
                problem_space.handled_failed_assertions.add(assertion)
                non_optional = cast(CharmEndpointNonOptionalTag, tag)
                assert non_optional.charm.endpoint is not None
                return self._add_charms_for_endpoint(
                    non_optional.charm.charm_id, non_optional.charm.endpoint, problem_space
                )
            if tag.kind == Assertions.APPLICATION_EXISTS:
                problem_space.handled_failed_assertions.add(assertion)
                app_exists = cast(ApplicationExistsTag, tag)
                return self._add_charm_for_application(app_exists.application, problem_space)

        raise UnresolvableBundleError(f"Cannot handle failed assertions: {failed_assertions}")

    def _add_charm_for_application(self, application: str, problem_space: ProblemSpace) -> ProblemSpace:
        # Get the charm matching the application constraints
        constraints = problem_space.application_constraints[application]
        charm = self.charmhub_client.charm_from_store(
            charm_name=constraints.charm,
            ubuntu_arch=problem_space.arch_constraint,
            charm_channel=constraints.channel,
            charm_revision=constraints.revision,
            ubuntu_version=constraints.base,
        )

        # Add the charm to the problem space
        return add_charm_to_problem_space(charm, problem_space)

    def _add_charms_for_endpoint(self, charm_id: int, endpoint_name: str, problem_space: ProblemSpace) -> ProblemSpace:
        try:
            return add_charms_for_endpoint(charm_id, endpoint_name, problem_space, self.charmhub_client)
        except ValueError as exc:
            raise UnresolvableBundleError(str(exc)) from exc

    def _optimize_solution(self, problem_space: ProblemSpace) -> z3.ModelRef:
        optimizer = z3.Optimize()
        add_constraints(optimizer, problem_space)

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
                    for charm in problem_space.charms
                ]
                + [z3.IntVal(0)]
            )
        )

        optimizer.minimize(
            z3.Sum(
                [z3.If(integration.exists, 1, 0) for integration in problem_space.charm_integrations.values()]
                + [z3.IntVal(0)]
            )
        )

        result = optimizer.check()
        if result != z3.sat:
            raise UnresolvableBundleError("Optimization failed - problem became unsatisfiable")

        return optimizer.model()
