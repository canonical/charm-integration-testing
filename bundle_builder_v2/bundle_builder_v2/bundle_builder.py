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

from .charm import ENDPOINT_PROVIDES, ENDPOINT_REQUIRES

from .bundle import Application, Bundle, Integration, ApplicationEndpoint
from .charmhub import CharmhubClient


class ProblemSpace(BaseModel):
    """Z3 variables representing the bundle building problem."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    
    app_vars: dict[str, z3.BoolRef] = Field(default_factory=dict)
    integration_vars: dict[Integration, z3.BoolRef] = Field(default_factory=dict)
    endpoint_integration_counts: dict[ApplicationEndpoint, z3.ArithRef] = Field(default_factory=dict)


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
        applications = base.applications.copy()

        while True:
            # Create Z3 solver
            solver = z3.Optimize()

            # Create problem space
            problem_space = self._create_problem_space(solver, applications)

            # Add constraints of the base bundle
            self._add_base_bundle_constraints(solver, problem_space, base)
            
            # # Add the extra constraints
            # self._add_constraints(solver, problem_space)

            # Add the objective function

            # Solve
            if solver.check() == z3.sat:
                model = solver.model()
                return self._extract_bundle_from_model(model, base, problem_space)
            
            # If unsat, find tracked down required integrations not yet in the bundle

            # And add these as potential applications
            

    def _create_problem_space(self, solver: z3.Optimize, applications: dict[str, Application]) -> ProblemSpace:
        """Create Z3 variables for the problem space."""
        problem_space = ProblemSpace()
        
        # Create app variables
        for app_name in applications.keys():
            problem_space.app_vars[app_name] = z3.Bool(f'app/{app_name}')
        
        # Create integration variables and constraints
        for app_name, app in applications.items():
            for endpoint_name, endpoint in app.charm.endpoints.items():
                app_endpoint = ApplicationEndpoint(application=app_name, endpoint=endpoint_name)
                count_var = z3.Int(f'count/{app_name}/{endpoint_name}')
                solver.add(count_var >= 0)
                problem_space.endpoint_integration_counts[app_endpoint] = count_var
                
                # Find compatible integrations
                if endpoint.type != ENDPOINT_PROVIDES:
                    continue
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
                        other_app_endpoint = ApplicationEndpoint(application=other_app_name, endpoint=other_endpoint_name)
                        
                        # Create integration variable
                        integration = Integration(
                            provider=app_endpoint,
                            requirer=other_app_endpoint,
                        )
                        int_var = z3.Bool(f'int/{app_name}/{endpoint_name}/{other_app_name}/{other_endpoint_name}')
                        problem_space.integration_vars[integration] = int_var
                        
                        # Integration exists, both apps exist
                        solver.add(z3.Implies(
                            int_var,
                            z3.And(
                                problem_space.app_vars[app_name],
                                problem_space.app_vars[other_app_name]
                            )
                        ))
                        
                        integrations.append(int_var)
                
                # Link count to integrations
                if integrations:
                    solver.add(count_var == z3.Sum([z3.If(v, 1, 0) for v in integrations]))
        
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
