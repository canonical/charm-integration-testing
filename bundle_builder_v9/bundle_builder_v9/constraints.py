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

from pydantic import BaseModel, ConfigDict

from .charm import CharmChannel


class IntegrationConstraint(BaseModel):
    """User-provided constraint specifying an integration between two application endpoints.

    This is an input constraint, not to be confused with Integration which is the output format.
    """

    model_config = ConfigDict(frozen=True)

    endpoint1: str  # Format: "application:endpoint"
    endpoint2: str  # Format: "application:endpoint"


class ApplicationEndpoint(BaseModel):
    """Represents an application and one of its endpoints."""

    model_config = ConfigDict(frozen=True)

    application: str
    endpoint: str


class CharmEndpoint(BaseModel):
    """Represents a charm instance and one of its endpoints."""

    model_config = ConfigDict(frozen=True)

    charm_id: int
    endpoint: str


class ApplicationIntegration(BaseModel):
    """Represents an integration between two application endpoints.

    Endpoints are unordered since we don't know which is requires/provides until charms are resolved.
    """

    model_config = ConfigDict(frozen=True)

    endpoint1: ApplicationEndpoint
    endpoint2: ApplicationEndpoint


class CharmIntegration(BaseModel):
    """Represents an integration between two charm endpoints.

    Endpoints are ordered semantically: requires comes before provides.
    """

    model_config = ConfigDict(frozen=True)

    requires_endpoint: CharmEndpoint
    provides_endpoint: CharmEndpoint


class ApplicationToCharmMapping(BaseModel):
    """Represents a mapping from an application to a charm instance."""

    model_config = ConfigDict(frozen=True)

    application: str
    charm_id: int


class ApplicationConstraint(BaseModel):
    charm: str
    channel: CharmChannel | None = None
    revision: int | None = None
    base: str | None = None
