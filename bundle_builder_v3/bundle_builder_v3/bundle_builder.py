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

from .bundle import ApplicationEndpoint, Bundle, Integration
from .charm import CharmChannel
from .charmhub import CharmhubClient


class UnsatInfo(BaseModel):
    """Information about why a bundle couldn't be satisfied."""

    missing_integrations: list[tuple[str, str, str]] = Field(default_factory=list)  # (app, endpoint, interface)


class ProblemSpace(BaseModel):
    """Z3 variables representing the bundle building problem."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Track if applications exist
    app_vars: dict[str, z3.BoolRef] = Field(default_factory=dict)

    # Track if integrations exist
    integration_vars: dict[Integration, z3.BoolRef] = Field(default_factory=dict)

    # Track the count of integrations per endpoint
    endpoint_integration_counts: dict[ApplicationEndpoint, z3.ArithRef] = Field(default_factory=dict)

    # # Track named constraints for unsat core analysis
    # constraint_names: dict[str, tuple[str, str, str]] = Field(
    #     default_factory=dict
    # )  # name -> (app, endpoint, interface)


class UnresolvableBundleError(Exception):
    def __init__(self, message: str, best_bundle: Bundle | None = None):
        super().__init__(message)
        self.best_bundle = best_bundle


class ApplicationConstraint(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    charm: str
    channel: CharmChannel | None = None
    revision: int | None = None
    base: str | None = None


class IntegrationConstraint(BaseModel):
    model_config = ConfigDict(frozen=True)

    application_1: str
    endpoint_1: str
    application_2: str
    endpoint_2: str


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
        application_constraints: set[ApplicationConstraint],
        integration_constraints: set[IntegrationConstraint],
        platform_constraint: str,
        arch_constraint: str,
    ) -> Bundle:
        self.logger.info(f"Application constraints: {application_constraints}")
        self.logger.info(f"Integration constraints: {integration_constraints}")
        self.logger.info(f"Platform constraint: {platform_constraint}")
        self.logger.info(f"Architecture constraint: {arch_constraint}")
        raise UnresolvableBundleError("Not implemented")
