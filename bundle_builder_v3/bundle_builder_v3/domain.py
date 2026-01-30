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

import z3
from pydantic import BaseModel, ConfigDict, Field

from .charm import Charm, CharmChannel, EndpointType


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


class DomainEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    count: z3.ArithRef


class DomainCharm(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    exists: z3.BoolRef
    spec: Charm
    endpoints: dict[str, DomainEndpoint]


class DomainCharmIntegration(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    exists: z3.BoolRef


class Domain(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    application_constraints: dict[str, ApplicationConstraint] = Field(default_factory=dict)
    integration_constraints: set[ApplicationIntegration] = Field(default_factory=set)
    arch_constraint: str
    platform_constraint: str

    application_to_charm: dict[ApplicationToCharmMapping, z3.BoolRef] = Field(default_factory=dict)
    application_integration_to_charm_integration: dict[tuple[ApplicationIntegration, CharmIntegration], z3.BoolRef] = (
        Field(default_factory=dict)
    )

    charms: list[DomainCharm] = Field(default_factory=list)
    charm_integrations: dict[CharmIntegration, DomainCharmIntegration] = Field(default_factory=dict)

    handled_failed_assertions: set[str] = Field(default_factory=set)


def initialize_domain(
    applications: dict[str, ApplicationConstraint],
    integrations: set[IntegrationConstraint],
    platform: str,
    arch: str,
) -> Domain:
    # Convert IntegrationConstraints to ApplicationIntegration models
    app_integrations = set()
    for integration in integrations:
        # Parse endpoint1 and endpoint2 (format: "application:endpoint")
        app1, ep1 = integration.endpoint1.split(":", 1)
        app2, ep2 = integration.endpoint2.split(":", 1)

        # Validate that both applications exist
        for app in [app1, app2]:
            if app not in applications:
                raise ValueError(
                    f"Integration references undefined application '{app}'. "
                    f"Available applications: {', '.join(sorted(applications.keys()))}"
                )

        # Use alphabetical ordering for consistency in set operations
        endpoints = [(app1, ep1), (app2, ep2)]
        sorted_eps = sorted(endpoints, key=lambda e: (e[0], e[1]))
        app_integrations.add(
            ApplicationIntegration(
                endpoint1=ApplicationEndpoint(application=sorted_eps[0][0], endpoint=sorted_eps[0][1]),
                endpoint2=ApplicationEndpoint(application=sorted_eps[1][0], endpoint=sorted_eps[1][1]),
            )
        )

    return Domain(
        application_constraints=applications,
        integration_constraints=app_integrations,
        platform_constraint=platform,
        arch_constraint=arch,
    )


def add_charm_to_domain(charm: Charm, domain: Domain) -> Domain:
    charm_id = len(domain.charms)
    domain.charms.append(
        DomainCharm(
            exists=z3.Bool(f"charm_{charm.name}_{charm_id}_exists"),
            spec=charm,
            endpoints={
                name: DomainEndpoint(count=z3.Int(f"charm_{charm.name}_{charm_id}_endpoint_{name}_count"))
                for name, endpoint in charm.endpoints.items()
            },
        )
    )

    for other_charm_id, other_charm in enumerate(domain.charms):
        if other_charm_id == charm_id:
            continue
        for endpoint_name, endpoint in charm.endpoints.items():
            for other_endpoint_name, other_endpoint in other_charm.spec.endpoints.items():
                if endpoint.interface != other_endpoint.interface:
                    continue
                # Create CharmIntegration with semantic ordering: requires before provides
                if endpoint.type == EndpointType.REQUIRES and other_endpoint.type == EndpointType.PROVIDES:
                    charm_integration = CharmIntegration(
                        requires_endpoint=CharmEndpoint(charm_id=charm_id, endpoint=endpoint_name),
                        provides_endpoint=CharmEndpoint(charm_id=other_charm_id, endpoint=other_endpoint_name),
                    )
                    domain.charm_integrations[charm_integration] = DomainCharmIntegration(
                        exists=z3.Bool(
                            f"charm_integration_{charm.name}_{charm_id}:{endpoint_name}__{other_charm.spec.name}_{other_charm_id}:{other_endpoint_name}_exists"
                        )
                    )
                elif endpoint.type == EndpointType.PROVIDES and other_endpoint.type == EndpointType.REQUIRES:
                    charm_integration = CharmIntegration(
                        requires_endpoint=CharmEndpoint(charm_id=other_charm_id, endpoint=other_endpoint_name),
                        provides_endpoint=CharmEndpoint(charm_id=charm_id, endpoint=endpoint_name),
                    )
                    domain.charm_integrations[charm_integration] = DomainCharmIntegration(
                        exists=z3.Bool(
                            f"charm_integration_{other_charm.spec.name}_{other_charm_id}:{other_endpoint_name}__{charm.name}_{charm_id}:{endpoint_name}_exists"
                        )
                    )

    for application, constraints in domain.application_constraints.items():
        if (
            constraints.charm != charm.name
            or (constraints.channel is not None and constraints.channel != charm.channel)
            or (constraints.revision is not None and constraints.revision != charm.revision)
            or (constraints.base is not None and constraints.base != charm.ubuntu_version)
        ):
            continue
        mapping = ApplicationToCharmMapping(application=application, charm_id=charm_id)
        domain.application_to_charm[mapping] = z3.Bool(f"app_{application}_maps_to_charm_{charm.name}_{charm_id}")

    for app_integration in domain.integration_constraints:
        for charm_integration in domain.charm_integrations:
            # Try both orderings since ApplicationIntegration is unordered
            charm_req_ep = charm_integration.requires_endpoint
            charm_prov_ep = charm_integration.provides_endpoint

            # Try ordering 1: endpoint1=requires, endpoint2=provides
            if (
                app_integration.endpoint1.endpoint == charm_req_ep.endpoint
                and app_integration.endpoint2.endpoint == charm_prov_ep.endpoint
            ):
                req_mapping = ApplicationToCharmMapping(
                    application=app_integration.endpoint1.application, charm_id=charm_req_ep.charm_id
                )
                prov_mapping = ApplicationToCharmMapping(
                    application=app_integration.endpoint2.application, charm_id=charm_prov_ep.charm_id
                )

                if req_mapping in domain.application_to_charm and prov_mapping in domain.application_to_charm:
                    domain.application_integration_to_charm_integration[(app_integration, charm_integration)] = z3.Bool(
                        f"app_integration_{app_integration.endpoint1.application}:{app_integration.endpoint1.endpoint}__{app_integration.endpoint2.application}:{app_integration.endpoint2.endpoint}_maps_to_charm_integration_{charm_req_ep.charm_id}:{charm_req_ep.endpoint}__{charm_prov_ep.charm_id}:{charm_prov_ep.endpoint}"
                    )

            # Try ordering 2: endpoint1=provides, endpoint2=requires
            elif (
                app_integration.endpoint1.endpoint == charm_prov_ep.endpoint
                and app_integration.endpoint2.endpoint == charm_req_ep.endpoint
            ):
                req_mapping = ApplicationToCharmMapping(
                    application=app_integration.endpoint2.application, charm_id=charm_req_ep.charm_id
                )
                prov_mapping = ApplicationToCharmMapping(
                    application=app_integration.endpoint1.application, charm_id=charm_prov_ep.charm_id
                )

                if req_mapping in domain.application_to_charm and prov_mapping in domain.application_to_charm:
                    domain.application_integration_to_charm_integration[(app_integration, charm_integration)] = z3.Bool(
                        f"app_integration_{app_integration.endpoint1.application}:{app_integration.endpoint1.endpoint}__{app_integration.endpoint2.application}:{app_integration.endpoint2.endpoint}_maps_to_charm_integration_{charm_req_ep.charm_id}:{charm_req_ep.endpoint}__{charm_prov_ep.charm_id}:{charm_prov_ep.endpoint}"
                    )

    return domain
