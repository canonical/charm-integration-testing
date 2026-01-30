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
from enum import Enum

import z3
from pydantic import BaseModel, ConfigDict, Field

from .bundle import Application, ApplicationEndpoint, Bundle, Integration
from .charm import Charm, CharmChannel, EndpointType
from .charmhub import CharmhubClient


class Assertions(str, Enum):
    APPLICATION_EXISTS = "application_exists"
    APPLICATION_INTEGRATION_EXISTS = "application_integration_exists"
    CHARM_ENDPOINT_NON_OPTIONAL = "charm_endpoint_non_optional"
    CHARM_MAPPED_TO_SINGLE_APPLICATION = "charm_mapped_to_single_application"
    CHARM_INTEGRATION_MAPPED_TO_SINGLE_APPLICATION_INTEGRATION = (
        "charm_integration_mapped_to_single_application_integration"
    )
    CHARM_EXISTS_FROM_APPLICATION = "charm_exists_from_application"
    CHARM_INTEGRATION_EXISTS_FROM_APPLICATION_INTEGRATION = "charm_integration_exists_from_application_integration"
    CHARM_EXISTS_FROM_INTEGRATION = "charm_exists_from_integration"
    ENDPOINT_COUNT_MATCHES_INTEGRATIONS = "endpoint_count_matches_integrations"
    ENDPOINT_RESPECTS_LIMIT = "endpoint_respects_limit"


class ApplicationConstraint(BaseModel):
    charm: str
    channel: CharmChannel | None = None
    revision: int | None = None
    base: str | None = None


class ProblemSpaceEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # type: EndpointType
    # interface: str
    # required: z3.BoolRef
    count: z3.ArithRef


class ProblemSpaceCharm(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    exists: z3.BoolRef
    spec: Charm
    endpoints: dict[str, ProblemSpaceEndpoint]


class ProblemSpaceCharmIntegration(BaseModel):
    """Possible integration between two charm endpoints."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    exists: z3.BoolRef


class ProblemSpace(BaseModel):
    """Z3 variables representing the bundle building problem."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Constraints from the user
    application_constraints: dict[str, ApplicationConstraint] = Field(default_factory=set)
    integration_constraints: set[frozenset[tuple[str, str]]] = Field(default_factory=set)
    arch_constraint: str
    platform_constraint: str

    # Mapping constraints from the requirements to the implementation space
    application_to_charm: dict[tuple[str, int], z3.BoolRef] = Field(default_factory=dict)
    application_integration_to_charm_integration: dict[
        tuple[frozenset[tuple[str, str]], frozenset[tuple[int, str]]], z3.BoolRef
    ] = Field(default_factory=dict)

    # Possible charm instances and possible integrations
    charms: list[ProblemSpaceCharm] = Field(default_factory=list)
    charm_integrations: dict[frozenset[tuple[int, str]], ProblemSpaceCharmIntegration] = Field(default_factory=dict)

    # Handled assertions
    handled_failed_assertions: set[str] = Field(default_factory=set)


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
        problem_space = self._initialize_problem_space(
            applications=applications,
            integrations=integrations,
            platform=platform,
            arch=arch,
        )

        # Iterative loop: expand problem space until satisfiable
        max_iterations = 100
        for iteration in range(max_iterations):
            self.logger.info(f"Iteration {iteration + 1}/{max_iterations}")

            # Create solver with unsat core tracking
            solver = z3.Solver()
            solver.set("unsat_core", True)

            # Add constraints
            self._add_constraints(solver, problem_space)

            # Check satisfiability
            result = solver.check()

            if result == z3.sat:
                self.logger.info("Problem is satisfiable!")
                self.logger.info("Re-solving with optimization to minimize charms and integrations...")
                model = self._optimize_solution(problem_space)
                return self._extract_bundle(model, problem_space)

            elif result == z3.unsat:
                self.logger.info("Problem is unsatisfiable, failed assertions")
                unsat_core = solver.unsat_core()
                for assertion in unsat_core:
                    self.logger.debug(f"  - {assertion}")
                problem_space = self._handle_failed_assertions(
                    [str(assertion) for assertion in unsat_core], problem_space
                )
            else:
                raise UnresolvableBundleError("Solver returned unknown")

        raise UnresolvableBundleError(f"Could not satisfy constraints after {max_iterations} iterations")

    def _initialize_problem_space(
        self,
        applications: dict[str, ApplicationConstraint],
        integrations: set[Integration],
        platform: str,
        arch: str,
    ) -> ProblemSpace:
        return ProblemSpace(
            application_constraints=applications,
            integration_constraints={
                frozenset({(ep.application, ep.endpoint) for ep in integration}) for integration in integrations
            },
            platform_constraint=platform,
            arch_constraint=arch,
        )

    def _handle_failed_assertions(
        self,
        failed_assertions: list[str],
        problem_space: ProblemSpace,
    ) -> ProblemSpace:
        # Handle an assertions that have not been previously handled
        for assertion in failed_assertions:
            if assertion in problem_space.handled_failed_assertions:
                continue
            key, metadata = assertion.split("::", 1)
            if key == Assertions.CHARM_ENDPOINT_NON_OPTIONAL:
                problem_space.handled_failed_assertions.add(assertion)
                charm_name, charm_id_str, endpoint = metadata.split(":")
                charm_id = int(charm_id_str)
                return self._add_charms_for_endpoint(charm_id, endpoint, problem_space)
            elif key == Assertions.APPLICATION_EXISTS:
                problem_space.handled_failed_assertions.add(assertion)
                application = metadata
                return self._add_charm_for_application(application, problem_space)

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
        return self._add_charm_to_problem_space(charm, problem_space)

    def _add_charms_for_endpoint(self, charm_id: int, endpoint: str, problem_space: ProblemSpace) -> ProblemSpace:
        # Find the charm in the problem space
        endpoint = problem_space.charms[charm_id].spec.endpoints[endpoint]

        # Request charms from charmhub that can fulfill this endpoint
        fulfilling_charms: set[str] = set()
        if endpoint.type == EndpointType.REQUIRES:
            fulfilling_charms = self.charmhub_client.find_charms(
                provides=endpoint.interface, platform=problem_space.platform_constraint
            )
        elif endpoint.type == EndpointType.PROVIDES:
            fulfilling_charms = self.charmhub_client.find_charms(
                requires=endpoint.interface, platform=problem_space.platform_constraint
            )

        # If no fulfilling charms abort
        if len(fulfilling_charms) == 0:
            raise UnresolvableBundleError(
                f"No charms found that can fulfill interface {endpoint.interface} for charm endpoint {problem_space.charms[charm_id].spec.name}:{endpoint}"
            )

        # Fetch each charm and add to problem space
        for charm in fulfilling_charms:
            spec = self.charmhub_client.charm_from_store(
                charm_name=charm,
                ubuntu_arch=problem_space.arch_constraint,
            )
            problem_space = self._add_charm_to_problem_space(spec, problem_space)

        return problem_space

    def _add_charm_to_problem_space(self, charm: Charm, problem_space: ProblemSpace) -> ProblemSpace:
        # Add charm instance
        charm_id = len(problem_space.charms)
        problem_space.charms.append(
            ProblemSpaceCharm(
                exists=z3.Bool(f"charm_{charm.name}_{charm_id}_exists"),
                spec=charm,
                endpoints={
                    name: ProblemSpaceEndpoint(count=z3.Int(f"charm_{charm.name}_{charm_id}_endpoint_{name}_count"))
                    for name, endpoint in charm.endpoints.items()
                },
            )
        )

        # Add charm integration instances with other charms
        for other_charm_id, other_charm in enumerate(problem_space.charms):
            # Do not integrate with self
            if other_charm_id == charm_id:
                continue

            for endpoint_name, endpoint in charm.endpoints.items():
                for other_endpoint_name, other_endpoint in other_charm.spec.endpoints.items():
                    # Only integrate same interface
                    if endpoint.interface != other_endpoint.interface:
                        continue
                    # Only integrate provides -> requires
                    integration_key = frozenset({(charm_id, endpoint_name), (other_charm_id, other_endpoint_name)})
                    if endpoint.type == EndpointType.PROVIDES and other_endpoint.type == EndpointType.REQUIRES:
                        problem_space.charm_integrations[integration_key] = ProblemSpaceCharmIntegration(
                            exists=z3.Bool(
                                f"charm_integration_{charm.name}_{charm_id}:{endpoint_name}__{other_charm.spec.name}_{other_charm_id}:{other_endpoint_name}_exists"
                            )
                        )
                    elif endpoint.type == EndpointType.REQUIRES and other_endpoint.type == EndpointType.PROVIDES:
                        problem_space.charm_integrations[integration_key] = ProblemSpaceCharmIntegration(
                            exists=z3.Bool(
                                f"charm_integration_{other_charm.spec.name}_{other_charm_id}:{other_endpoint_name}__{charm.name}_{charm_id}:{endpoint_name}_exists"
                            )
                        )

        # Add possible application mappings
        for application, constraints in problem_space.application_constraints.items():
            # Check constraints
            if (
                constraints.charm != charm.name
                or (constraints.channel is not None and constraints.channel != charm.channel)
                or (constraints.revision is not None and constraints.revision != charm.revision)
                or (constraints.base is not None and constraints.base != charm.ubuntu_version)
            ):
                continue
            problem_space.application_to_charm[(application, charm_id)] = z3.Bool(
                f"app_{application}_maps_to_charm_{charm.name}_{charm_id}"
            )

        # Add possible application integration mappings
        for integration in problem_space.integration_constraints:
            app_ep_1, app_ep_2 = sorted(integration)
            for charm_integration in problem_space.charm_integrations:
                charm_ep_1, charm_ep_2 = sorted(charm_integration)
                if {app_ep_1[1], app_ep_2[1]} != {charm_ep_1[1], charm_ep_2[1]}:
                    continue
                problem_space.application_integration_to_charm_integration[(integration, charm_integration)] = z3.Bool(
                    f"app_integration_{app_ep_1[0]}:{app_ep_1[1]}__{app_ep_2[0]}:{app_ep_2[1]}_maps_to_charm_integration_{charm_ep_1[0]}:{charm_ep_1[1]}__{charm_ep_2[0]}:{charm_ep_2[1]}"
                )

        return problem_space

    def _add_constraints(self, solver: z3.Solver, problem_space: ProblemSpace) -> None:
        self._add_application_constraints(solver, problem_space)
        self._add_charm_constraints(solver, problem_space)
        # self._add_integration_mapping_constraints(solver, problem_space)

    def _add_application_constraints(self, solver: z3.Solver, problem_space: ProblemSpace) -> None:
        # Ensure application maps to one and only one charm
        for application in problem_space.application_constraints.keys():
            solver.assert_and_track(
                z3.Sum(
                    [z3.If(m, 1, 0) for (a, c), m in problem_space.application_to_charm.items() if a == application]
                    + [z3.IntVal(0)]
                )
                == 1,
                f"{Assertions.APPLICATION_EXISTS}::{application}",
            )
        # Ensure charms are only mapped to at most one application
        for charm_id, charm in enumerate(problem_space.charms):
            solver.assert_and_track(
                z3.Sum(
                    [z3.If(m, 1, 0) for (a, c), m in problem_space.application_to_charm.items() if c == charm_id]
                    + [z3.IntVal(0)]
                )
                <= 1,
                f"{Assertions.CHARM_MAPPED_TO_SINGLE_APPLICATION}::{charm.spec.name}:{charm_id}",
            )
        # Ensure charms mapped from applications exist
        for (application, charm_id), mapping_var in problem_space.application_to_charm.items():
            charm_var = problem_space.charms[charm_id].exists
            solver.assert_and_track(
                z3.Implies(mapping_var, charm_var),
                f"{Assertions.CHARM_EXISTS_FROM_APPLICATION}::{application}::{problem_space.charms[charm_id].spec.name}:{charm_id}",
            )
        # Ensure application integrations map to one and only one charm integration
        for application_integration in problem_space.integration_constraints:
            solver.assert_and_track(
                z3.Sum(
                    [
                        z3.If(m, 1, 0)
                        for (a_ep, c_ep), m in problem_space.application_integration_to_charm_integration.items()
                        if a_ep == application_integration
                    ]
                    + [z3.IntVal(0)]
                )
                == 1,
                f"{Assertions.APPLICATION_INTEGRATION_EXISTS}::{application_integration}",
            )
        # Ensure charm integrations are only mapped to at most one application integration
        for charm_integration in problem_space.charm_integrations.keys():
            solver.assert_and_track(
                z3.Sum(
                    [
                        z3.If(m, 1, 0)
                        for (a_ep, c_ep), m in problem_space.application_integration_to_charm_integration.items()
                        if c_ep == charm_integration
                    ]
                    + [z3.IntVal(0)]
                )
                <= 1,
                f"{Assertions.CHARM_INTEGRATION_MAPPED_TO_SINGLE_APPLICATION_INTEGRATION}::{charm_integration}",
            )
        # Ensure integrations mapped from applications exist
        for (
            application_integration,
            charm_integration,
        ), mapping_var in problem_space.application_integration_to_charm_integration.items():
            charm_integration_var = problem_space.charm_integrations[charm_integration].exists
            app_ep_1, app_ep_2 = sorted(application_integration)
            charm_ep_1, charm_ep_2 = sorted(charm_integration)
            solver.assert_and_track(
                z3.Implies(mapping_var, charm_integration_var),
                f"{Assertions.CHARM_INTEGRATION_EXISTS_FROM_APPLICATION_INTEGRATION}::{app_ep_1[0]}:{app_ep_1[1]}--{app_ep_2[0]}:{app_ep_2[1]}::{charm_ep_1[0]}:{charm_ep_1[1]}--{charm_ep_2[0]}:{charm_ep_2[1]}",
            )

    def _add_charm_constraints(self, solver: z3.Solver, problem_space: ProblemSpace) -> None:
        # Ensure charms that have integrations exist
        for integration_key, integration_var in problem_space.charm_integrations.items():
            charm_ids = [charm_id for (charm_id, endpoint_name) in integration_key]
            for charm_id in charm_ids:
                charm_var = problem_space.charms[charm_id].exists
                charm_ep_1, charm_ep_2 = sorted(integration_key)
                solver.assert_and_track(
                    z3.Implies(integration_var.exists, charm_var),
                    f"{Assertions.CHARM_EXISTS_FROM_INTEGRATION}::{problem_space.charms[charm_id].spec.name}:{charm_id}::{charm_ep_1[0]}:{charm_ep_1[1]}--{charm_ep_2[0]}:{charm_ep_2[1]}",
                )
        # Calculate charm endpoint count from integrations
        for charm_id, charm in enumerate(problem_space.charms):
            for endpoint_name, endpoint in charm.endpoints.items():
                # An endpoint is used if any integration using it exists
                integrations_using_endpoint = []
                for integration_key, integration_var in problem_space.charm_integrations.items():
                    if (charm_id, endpoint_name) in integration_key:
                        integrations_using_endpoint.append(integration_var.exists)
                solver.assert_and_track(
                    endpoint.count == z3.Sum([z3.If(i, 1, 0) for i in integrations_using_endpoint] + [z3.IntVal(0)]),
                    f"{Assertions.ENDPOINT_COUNT_MATCHES_INTEGRATIONS}::{charm.spec.name}:{charm_id}:{endpoint_name}",
                )
        # Add non-optional endpoints from metadata (if charm exists)
        for charm_id, charm in enumerate(problem_space.charms):
            for endpoint_name, endpoint in charm.spec.endpoints.items():
                if not endpoint.optional:
                    solver.assert_and_track(
                        z3.Implies(charm.exists, charm.endpoints[endpoint_name].count >= 1),
                        f"{Assertions.CHARM_ENDPOINT_NON_OPTIONAL}::{charm.spec.name}:{charm_id}:{endpoint_name}",
                    )
        # Add limits from metadata (if charm exists)
        for charm_id, charm in enumerate(problem_space.charms):
            for endpoint_name, endpoint in charm.spec.endpoints.items():
                if endpoint.limit is not None:
                    solver.assert_and_track(
                        z3.Implies(charm.exists, charm.endpoints[endpoint_name].count <= endpoint.limit),
                        f"{Assertions.ENDPOINT_RESPECTS_LIMIT}::{charm.spec.name}:{charm_id}:{endpoint_name}:{endpoint.limit}",
                    )

    def _optimize_solution(self, problem_space: ProblemSpace) -> z3.ModelRef:
        """Re-solve with optimization to minimize the number of charms and integrations."""
        optimizer = z3.Optimize()

        # Add all constraints
        self._add_constraints(optimizer, problem_space)

        # Minimize weighted charm cost: higher priority => lower cost (1 / priority)
        optimizer.minimize(
            z3.Sum(
                [
                    z3.If(
                        charm.exists,
                        z3.RealVal(1.0 / max(charm.spec.priority, 1e-6)),
                        z3.RealVal(0),
                    )
                    for charm in problem_space.charms
                ]
                + [z3.RealVal(0)]
            )
        )

        # Then minimize number of integrations that exist
        optimizer.minimize(
            z3.Sum(
                [z3.If(integration.exists, 1, 0) for integration in problem_space.charm_integrations.values()]
                + [z3.IntVal(0)]
            )
        )

        # Solve
        result = optimizer.check()
        if result != z3.sat:
            raise UnresolvableBundleError("Optimization failed - problem became unsatisfiable")

        return optimizer.model()

    def _extract_bundle(self, model: z3.ModelRef, problem_space: ProblemSpace) -> Bundle:
        """Extract a Bundle from the Z3 model solution."""

        # Find all charms that exist in the solution
        existing_charm_ids = []
        for charm_id, charm in enumerate(problem_space.charms):
            if model.evaluate(charm.exists, model_completion=True):
                existing_charm_ids.append(charm_id)

        # Build mapping from charm_id to application name
        charm_id_to_app_name = {}
        used_names = set()

        # First, assign names from explicit application mappings
        for charm_id in existing_charm_ids:
            for (application, mapped_charm_id), mapping_var in problem_space.application_to_charm.items():
                if mapped_charm_id == charm_id and model.evaluate(mapping_var, model_completion=True):
                    charm_id_to_app_name[charm_id] = application
                    used_names.add(application)
                    self.logger.info(
                        f"Application '{application}' mapped to charm {problem_space.charms[charm_id].spec.name} (id={charm_id})"
                    )
                    break

        # Then, generate names for unmapped charms
        for charm_id in existing_charm_ids:
            if charm_id in charm_id_to_app_name:
                continue

            # Generate unique name: charm-name, charm-name-a, charm-name-b, etc.
            base_name = problem_space.charms[charm_id].spec.name
            app_name = base_name
            suffix_ord = ord("a")
            while app_name in used_names:
                app_name = f"{base_name}-{chr(suffix_ord)}"
                suffix_ord += 1

            charm_id_to_app_name[charm_id] = app_name
            used_names.add(app_name)
            self.logger.info(f"Application '{app_name}' generated for unmapped charm {base_name} (id={charm_id})")

        # Build applications dict
        applications = {}
        for charm_id, app_name in charm_id_to_app_name.items():
            applications[app_name] = Application(
                charm=problem_space.charms[charm_id].spec,
            )

        # Extract all integrations that exist
        integrations = set()
        for integration_key, integration_var in problem_space.charm_integrations.items():
            if model.evaluate(integration_var.exists, model_completion=True):
                charm_ep_1, charm_ep_2 = sorted(integration_key)
                charm_id_1, endpoint_1 = charm_ep_1
                charm_id_2, endpoint_2 = charm_ep_2

                app_name_1 = charm_id_to_app_name.get(charm_id_1)
                app_name_2 = charm_id_to_app_name.get(charm_id_2)

                if app_name_1 and app_name_2:
                    integration = frozenset(
                        {
                            ApplicationEndpoint(application=app_name_1, endpoint=endpoint_1),
                            ApplicationEndpoint(application=app_name_2, endpoint=endpoint_2),
                        }
                    )
                    integrations.add(integration)
                    self.logger.info(f"Integration {app_name_1}:{endpoint_1} -- {app_name_2}:{endpoint_2}")

        return Bundle(
            applications=applications,
            integrations=integrations,
            platform=problem_space.platform_constraint,
            arch=problem_space.arch_constraint,
        )
