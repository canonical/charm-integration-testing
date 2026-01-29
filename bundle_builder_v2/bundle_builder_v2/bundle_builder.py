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

import z3
from pydantic import BaseModel, ConfigDict, Field

from .bundle import Application, ApplicationEndpoint, Bundle, Integration
from .charm import ENDPOINT_PROVIDES, ENDPOINT_REQUIRES
from .charmhub import CharmhubClient
from .scriptlet_invoker import ParsedConstraint, ScriptletInvoker, parse_error_code_rejection


class UnsatInfo(BaseModel):
    """Information about why a bundle couldn't be satisfied."""

    missing_integrations: list[tuple[str, str, str]] = Field(default_factory=list)  # (app, endpoint, interface)


class ProblemSpace(BaseModel):
    """Z3 variables representing the bundle building problem."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    app_vars: dict[str, z3.BoolRef] = Field(default_factory=dict)
    integration_vars: dict[Integration, z3.BoolRef] = Field(default_factory=dict)
    endpoint_integration_counts: dict[ApplicationEndpoint, z3.ArithRef] = Field(default_factory=dict)
    # Track named constraints for unsat core analysis
    constraint_names: dict[str, tuple[str, str, str]] = Field(
        default_factory=dict
    )  # name -> (app, endpoint, interface)


class UnresolvableBundleError(Exception):
    def __init__(self, message: str, best_bundle: Bundle):
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

    # Build out the bundle, pulling in charms that fulfill non-optional hanging required integrations
    def build(self, base: Bundle) -> Bundle:
        self.logger.info(f"Starting bundle build with {len(base.applications)} base applications")
        # Outer loop: problem space expansion
        applications = base.applications.copy()
        outer_iteration = 0
        max_outer_iterations = 5  # Prevent infinite loops

        while True:
            outer_iteration += 1
            self.logger.info(f"Outer loop iteration {outer_iteration}")

            if outer_iteration > max_outer_iterations:
                raise UnresolvableBundleError(
                    f"Cannot find valid bundle after {max_outer_iterations} outer iterations. "
                    "May need app discovery or problem space expansion.",
                    best_bundle=base,
                )
            # 1. Construct the problem space from the current set of applications
            solver = z3.Optimize()
            problem_space = self._create_problem_space(solver, applications)
            self.logger.info(
                f"Created problem space with {len(problem_space.app_vars)} app vars, {len(problem_space.integration_vars)} integration vars"
            )

            # 2. Add constraints from the base bundle (apps and integrations that must exist)
            self._add_base_bundle_constraints(solver, problem_space, base)
            self.logger.info("Added base bundle constraints")

            # Add objective function to minimize number of applications
            app_count = z3.Sum([z3.If(v, 1, 0) for v in problem_space.app_vars.values()])
            solver.minimize(app_count)

            # Inner loop: Z3 solve + scriptlet validation
            iteration = 0
            while True:
                iteration += 1
                self.logger.info(f"Inner loop iteration {iteration}")

                # 1. Z3 solve to find a valid bundle
                self.logger.info("Solving Z3 constraints...")
                result = solver.check()
                self.logger.info(f"Z3 result: {result}")

                if result == z3.sat:
                    # 2. If sat, check if bundle passes scriptlets
                    model = solver.model()
                    self.logger.info("Extracting bundle from Z3 model...")
                    candidate_bundle = self._extract_bundle_from_model(model, applications, problem_space, base)

                    self.logger.info(
                        f"Z3 found candidate bundle with {len(candidate_bundle.applications)} apps: {list(candidate_bundle.applications.keys())}"
                    )

                    # Validate with scriptlets
                    self.logger.info("Validating bundle with scriptlets...")
                    rejections = self._validate_with_scriptlets(candidate_bundle)

                    if not rejections:
                        # Bundle is valid!
                        self.logger.info("Found valid bundle!")
                        return candidate_bundle

                    # 3. For any rejections, add the constraints from scriptlet rejections
                    self.logger.info(f"Bundle rejected by {len(rejections)} scriptlet(s), adding constraints")
                    for app_name, constraint in rejections:
                        self.logger.info(f"  Adding constraint for {app_name}: {constraint.constraint_type}")
                        self._apply_constraint(solver, problem_space, app_name, constraint)

                    # 4. Resolve with Z3 again and repeat
                    continue

                elif result == z3.unsat:
                    # 5. If Z3 returns unsat, we need to add more apps to satisfy requirements
                    self.logger.info("Z3 returned UNSAT, need to expand problem space")

                    # 6. Extract unsat core to find what's missing
                    unsat_info = self._analyze_unsat_core(solver, problem_space, applications)
                    self.logger.info(f"Missing integrations: {unsat_info.missing_integrations}")

                    # 7. Discover and add apps that can satisfy the missing integrations
                    if not unsat_info.missing_integrations:
                        raise UnresolvableBundleError(
                            "Cannot find valid bundle topology (no missing integrations identified)", best_bundle=base
                        )

                    new_apps_added = self._discover_and_add_apps(unsat_info, applications, base)

                    if not new_apps_added:
                        raise UnresolvableBundleError(
                            f"Cannot find charms to satisfy missing integrations: {unsat_info.missing_integrations}",
                            best_bundle=base,
                        )

                    self.logger.info(f"Added {new_apps_added} new apps, restarting outer loop")
                    # Break inner loop to restart outer loop with expanded problem space
                    break

                else:
                    # Unknown result
                    raise RuntimeError(f"Z3 returned unexpected result: {result}")

    def _create_problem_space(self, solver: z3.Optimize, applications: dict[str, Application]) -> ProblemSpace:
        """Create Z3 variables for the problem space."""
        problem_space = ProblemSpace()

        # Create app variables
        for app_name in applications.keys():
            problem_space.app_vars[app_name] = z3.Bool(f"app/{app_name}")

        # Create integration variables and constraints
        for app_name, app in applications.items():
            for endpoint_name, endpoint in app.charm.endpoints.items():
                app_endpoint = ApplicationEndpoint(application=app_name, endpoint=endpoint_name)
                count_var = z3.Int(f"count/{app_name}/{endpoint_name}")
                solver.add(count_var >= 0)
                problem_space.endpoint_integration_counts[app_endpoint] = count_var

        # Create integration variables (only from PROVIDES side to avoid duplicates)
        for app_name, app in applications.items():
            for endpoint_name, endpoint in app.charm.endpoints.items():
                if endpoint.type != ENDPOINT_PROVIDES:
                    continue

                app_endpoint = ApplicationEndpoint(application=app_name, endpoint=endpoint_name)
                provider_count_var = problem_space.endpoint_integration_counts[app_endpoint]
                integrations = []

                for other_app_name, other_app in applications.items():
                    if other_app_name == app_name:
                        continue

                    for other_endpoint_name, other_endpoint in other_app.charm.endpoints.items():
                        # Check if possible integration
                        if other_endpoint.type != ENDPOINT_REQUIRES:
                            continue
                        if other_endpoint.interface != endpoint.interface:
                            continue
                        other_app_endpoint = ApplicationEndpoint(
                            application=other_app_name, endpoint=other_endpoint_name
                        )

                        # Create integration variable
                        integration = Integration(
                            provider=app_endpoint,
                            requirer=other_app_endpoint,
                        )
                        int_var = z3.Bool(f"int/{app_name}/{endpoint_name}/{other_app_name}/{other_endpoint_name}")
                        problem_space.integration_vars[integration] = int_var

                        # Integration exists => both apps exist
                        solver.add(
                            z3.Implies(
                                int_var,
                                z3.And(problem_space.app_vars[app_name], problem_space.app_vars[other_app_name]),
                            )
                        )

                        integrations.append(int_var)

                        # Also update the requirer's count
                        requirer_count_var = problem_space.endpoint_integration_counts[other_app_endpoint]
                        # The requirer's count should include this integration
                        # We'll handle this after we've collected all integrations

                # Link provider count to integrations
                if integrations:
                    solver.add(provider_count_var == z3.Sum([z3.If(v, 1, 0) for v in integrations]))
                else:
                    # No integrations available for this endpoint - count must be 0
                    solver.add(provider_count_var == 0)

        # Now link requirer counts to integrations
        for app_name, app in applications.items():
            for endpoint_name, endpoint in app.charm.endpoints.items():
                if endpoint.type != ENDPOINT_REQUIRES:
                    continue

                app_endpoint = ApplicationEndpoint(application=app_name, endpoint=endpoint_name)
                requirer_count_var = problem_space.endpoint_integration_counts[app_endpoint]

                # Find all integrations where this endpoint is the requirer
                requirer_integrations = []
                for integration, int_var in problem_space.integration_vars.items():
                    if integration.requirer == app_endpoint:
                        requirer_integrations.append(int_var)

                # Link requirer count to integrations
                if requirer_integrations:
                    solver.add(requirer_count_var == z3.Sum([z3.If(v, 1, 0) for v in requirer_integrations]))
                else:
                    # No integrations available for this endpoint - count must be 0
                    solver.add(requirer_count_var == 0)

        return problem_space

    def _add_base_bundle_constraints(
        self,
        solver: z3.Optimize,
        problem_space: ProblemSpace,
        base: Bundle,
    ) -> None:
        """Add constraints from the base bundle."""
        # Ensure all base apps are included
        for app_name in base.applications.keys():
            solver.add(problem_space.app_vars[app_name] == True)

        # Ensure all base integrations are included
        for integration in base.integrations:
            if integration in problem_space.integration_vars:
                solver.add(problem_space.integration_vars[integration] == True)

    def _validate_with_scriptlets(self, bundle: Bundle) -> list[tuple[str, ParsedConstraint]]:
        """
        Validate bundle with all scriptlets.

        Returns list of (app_name, constraint) tuples for rejections.
        """
        self.logger.info(f"Validating bundle with {len(bundle.applications)} apps")
        rejections = []

        for app_name, app in bundle.applications.items():
            charm = app.charm

            # Skip if charm has no scriptlet
            if not charm.scriptlet:
                self.logger.debug(f"  {app_name}: no scriptlet, skipping")
                continue

            self.logger.info(f"  Validating {app_name} with scriptlet")

            try:
                # Build relations dict for this app from bundle integrations
                relations = self._build_relations_for_app(app_name, bundle)

                # Build charm names dict for capability checking
                charm_names = {app_name: app.charm.name for app_name, app in bundle.applications.items()}

                self.logger.debug(f"    Relations: {relations}")

                # Create invoker and fire validate event
                invoker = ScriptletInvoker(charm.scriptlet, logger=self.logger)
                rejection = invoker.fire_validate_event(relations, charm_names)

                if not rejection:
                    self.logger.info(f"    {app_name}: ACCEPTED")
                    continue

                # Parse the rejection into a structured constraint
                constraint = parse_error_code_rejection(rejection)
                if not constraint:
                    self.logger.warning(f"Scriptlet for {app_name} returned legacy rejection: {rejection.reason}")
                    continue

                self.logger.info(f"    {app_name}: REJECTED - {constraint.constraint_type}")
                rejections.append((app_name, constraint))

            except Exception as e:
                self.logger.warning(f"Failed to validate {app_name} with scriptlet: {e}", exc_info=True)

        return rejections

    def _build_relations_for_app(self, app_name: str, bundle: Bundle) -> dict[str, list[str]]:
        """
        Build relations dict for an app from bundle integrations.

        Returns dict mapping endpoint names to lists of related application names.
        """
        relations: dict[str, list[str]] = {}

        for integration in bundle.integrations:
            # Check if this app is the provider
            if integration.provider.application == app_name:
                endpoint = integration.provider.endpoint
                related_app = integration.requirer.application
                if endpoint not in relations:
                    relations[endpoint] = []
                relations[endpoint].append(related_app)

            # Check if this app is the requirer
            elif integration.requirer.application == app_name:
                endpoint = integration.requirer.endpoint
                related_app = integration.provider.application
                if endpoint not in relations:
                    relations[endpoint] = []
                relations[endpoint].append(related_app)

        return relations

    def _apply_constraint(
        self,
        solver: z3.Optimize,
        problem_space: ProblemSpace,
        app_name: str,
        constraint: ParsedConstraint,
    ) -> None:
        """Convert a ParsedConstraint into Z3 solver constraints."""
        constraint_type = constraint.constraint_type

        if constraint_type == "required":
            # Required endpoint must have at least N integrations (default N=1)
            required_endpoint = constraint.required_endpoint
            min_count = constraint.min or 1  # Default to 1 if not specified

            if not required_endpoint:
                self.logger.warning(f"Required constraint missing endpoint for {app_name}")
                return

            app_endpoint = ApplicationEndpoint(application=app_name, endpoint=required_endpoint)

            if app_endpoint in problem_space.endpoint_integration_counts:
                count_var = problem_space.endpoint_integration_counts[app_endpoint]
                # Create a named constraint for unsat core tracking
                constraint_name = f"required_{app_name}_{required_endpoint}"
                z3_constraint = count_var >= min_count
                solver.assert_and_track(z3_constraint, constraint_name)

                self.logger.debug(f"Added required constraint: {app_name}:{required_endpoint} >= {min_count}")

                # Store constraint metadata
                problem_space.constraint_names[constraint_name] = (
                    app_name,
                    required_endpoint,
                    "unknown",  # Interface will be filled in by _analyze_unsat_core
                )
            else:
                self.logger.warning(
                    f"Endpoint {app_endpoint} not found in problem space - cannot add required constraint"
                )

        elif constraint_type == "mutual_exclusion":
            # Mutually exclusive endpoints - at most one can have integrations
            endpoints = constraint.conflicting_endpoints or []
            if len(endpoints) < 2:
                self.logger.warning(f"Mutual exclusion constraint needs at least 2 endpoints for {app_name}")
                return

            # Add pairwise constraints: if endpoint A has relation, endpoint B cannot
            for i, ep1 in enumerate(endpoints):
                for ep2 in endpoints[i + 1 :]:
                    ep1_endpoint = ApplicationEndpoint(application=app_name, endpoint=ep1)
                    ep2_endpoint = ApplicationEndpoint(application=app_name, endpoint=ep2)

                    if (
                        ep1_endpoint in problem_space.endpoint_integration_counts
                        and ep2_endpoint in problem_space.endpoint_integration_counts
                    ):
                        count1 = problem_space.endpoint_integration_counts[ep1_endpoint]
                        count2 = problem_space.endpoint_integration_counts[ep2_endpoint]
                        # At least one must be 0
                        solver.add(z3.Or(count1 == 0, count2 == 0))

        elif constraint_type == "limit":
            # Limit on number of integrations for an endpoint
            endpoint = constraint.endpoint
            max_count = constraint.max

            if not endpoint or max_count is None:
                self.logger.warning(f"Limit constraint missing endpoint or max for {app_name}")
                return

            app_endpoint = ApplicationEndpoint(application=app_name, endpoint=endpoint)
            if app_endpoint in problem_space.endpoint_integration_counts:
                count_var = problem_space.endpoint_integration_counts[app_endpoint]
                solver.add(count_var <= max_count)

        elif constraint_type == "conditional":
            # Conditional - at least one of the acceptable endpoints must have an integration
            endpoints = constraint.acceptable_endpoints or []
            if not endpoints:
                self.logger.warning(f"Conditional constraint missing endpoints for {app_name}")
                return

            # Create Z3 constraints: at least one endpoint must have count >= 1
            count_vars = []
            for ep in endpoints:
                app_endpoint = ApplicationEndpoint(application=app_name, endpoint=ep)
                if app_endpoint in problem_space.endpoint_integration_counts:
                    count_vars.append(problem_space.endpoint_integration_counts[app_endpoint] >= 1)

            if count_vars:
                solver.add(z3.Or(*count_vars))

        elif constraint_type == "capability":
            # Capability requirement - endpoint must integrate with specific charms
            # This is enforced by scriptlet validation, but we log it for visibility
            endpoint = constraint.endpoint
            required_charms = constraint.required_charms or []
            self.logger.info(f"Capability constraint for {app_name}:{endpoint} requires charms: {required_charms}")
            # Note: The actual enforcement happens during scriptlet validation
            # We could add Z3 constraints to filter integration variables, but that would require
            # knowing capabilities ahead of time. Current approach: validate after Z3 proposes solution.

        else:
            self.logger.debug(f"Skipping unsupported constraint type: {constraint_type}")

    def _analyze_unsat_core(
        self,
        solver: z3.Optimize,
        problem_space: ProblemSpace,
        applications: dict[str, Application],
    ) -> UnsatInfo:
        """Analyze the unsat core to determine what integrations are missing."""
        unsat_info = UnsatInfo()

        # Get the unsat core - these are the named constraints that conflict
        try:
            core = solver.unsat_core()
            self.logger.debug(f"UNSAT core size: {len(core)}")

            for constraint_name in core:
                constraint_str = str(constraint_name)
                self.logger.debug(f"UNSAT core constraint: {constraint_str}")

                # Check if this is one of our tracked required constraints
                if constraint_str in problem_space.constraint_names:
                    app_name, endpoint_name, _ = problem_space.constraint_names[constraint_str]

                    # Look up the interface for this endpoint
                    if app_name in applications:
                        app = applications[app_name]
                        if endpoint_name in app.charm.endpoints:
                            endpoint = app.charm.endpoints[endpoint_name]
                            interface = endpoint.interface

                            unsat_info.missing_integrations.append((app_name, endpoint_name, interface))
                            self.logger.info(
                                f"Need integration for {app_name}:{endpoint_name} "
                                f"(interface: {interface}, type: {endpoint.type})"
                            )
        except Exception as e:
            self.logger.warning(f"Failed to analyze unsat core: {e}")

        return unsat_info

    def _discover_and_add_apps(
        self,
        unsat_info: UnsatInfo,
        applications: dict[str, Application],
        base: Bundle,
    ) -> int:
        """Discover charms that can satisfy missing integrations and add them to applications.

        Returns the number of new apps added.
        """
        added_count = 0

        for app_name, endpoint_name, interface in unsat_info.missing_integrations:
            # Determine if we need a provider or requirer
            app = applications[app_name]
            endpoint = app.charm.endpoints[endpoint_name]

            if endpoint.type == ENDPOINT_REQUIRES:
                # We need a charm that provides this interface
                self.logger.info(f"Searching for charms that provide '{interface}' interface")
                charm_names = self.charmhub_client.find_charms(provides=interface, platform=base.platform)
            elif endpoint.type == ENDPOINT_PROVIDES:
                # We need a charm that requires this interface
                self.logger.info(f"Searching for charms that require '{interface}' interface")
                charm_names = self.charmhub_client.find_charms(requires=interface, platform=base.platform)
            else:
                self.logger.warning(f"Unsupported endpoint type: {endpoint.type}")
                continue

            if not charm_names:
                self.logger.warning(f"No charms found for interface '{interface}'")
                continue

            self.logger.info(f"Found {len(charm_names)} candidate charms: {list(charm_names)[:5]}...")

            # Add the first compatible charm (TODO: smarter selection)
            for charm_name in sorted(charm_names):  # Sort for determinism
                # Skip if already in applications
                if charm_name in applications:
                    continue

                try:
                    # Get the charm details - use the same base as the requesting app
                    requesting_app_charm = app.charm
                    charm = self.charmhub_client.charm_from_store(
                        charm_name=charm_name,
                        ubuntu_arch=base.arch,
                        ubuntu_version=requesting_app_charm.ubuntu_version,
                    )

                    # Create application with this charm
                    new_app = Application(charm=charm)

                    applications[charm_name] = new_app
                    self.logger.info(f"Added application '{charm_name}' to satisfy {app_name}:{endpoint_name}")
                    added_count += 1
                    break  # Only add one charm per missing integration

                except Exception as e:
                    self.logger.warning(f"Failed to add charm {charm_name}: {e}")
                    continue

        return added_count

    def _extract_bundle_from_model(
        self,
        model: z3.ModelRef,
        applications: dict[str, Application],
        problem_space: ProblemSpace,
        base: Bundle,
    ) -> Bundle:
        """Extract a Bundle from the Z3 solver's solution model."""
        # Extract applications that are True in the model
        selected_applications = {}
        for app_name, app_var in problem_space.app_vars.items():
            if model.eval(app_var, model_completion=True):
                # This app is included in the solution
                selected_applications[app_name] = applications[app_name]
                self.logger.debug(f"Solution includes application: {app_name}")

        # Extract integrations that are True in the model
        selected_integrations = set()
        for integration, int_var in problem_space.integration_vars.items():
            if model.eval(int_var, model_completion=True):
                # This integration is included in the solution
                selected_integrations.add(integration)
                self.logger.debug(
                    f"Solution includes integration: {integration.requirer.application}:{integration.requirer.endpoint} -> {integration.provider.application}:{integration.provider.endpoint}"
                )

        # Log endpoint integration counts from the solution
        for app_endpoint, count_var in problem_space.endpoint_integration_counts.items():
            count = model.eval(count_var, model_completion=True)
            if count.as_long() > 0:
                self.logger.debug(
                    f"Endpoint {app_endpoint.application}:{app_endpoint.endpoint} has {count} integration(s)"
                )

        # Construct and return the Bundle
        return Bundle(
            applications=selected_applications,
            integrations=selected_integrations,
            platform=base.platform,
            arch=base.arch,
        )
