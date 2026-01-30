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

import z3
from pydantic import BaseModel, ConfigDict, Field

from .bundle import Integration
from .charm import Charm, CharmChannel, EndpointType
from .charmhub import CharmhubClient


class ApplicationConstraint(BaseModel):
    charm: str
    channel: CharmChannel | None = None
    revision: int | None = None
    base: str | None = None


class ProblemSpaceEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    count: z3.ArithRef


class ProblemSpaceCharm(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    exists: z3.BoolRef
    spec: Charm
    endpoints: dict[str, ProblemSpaceEndpoint]


class ProblemSpaceCharmIntegration(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    exists: z3.BoolRef


class ProblemSpace(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    application_constraints: dict[str, ApplicationConstraint] = Field(default_factory=dict)
    integration_constraints: set[frozenset[tuple[str, str]]] = Field(default_factory=set)
    arch_constraint: str
    platform_constraint: str

    application_to_charm: dict[tuple[str, int], z3.BoolRef] = Field(default_factory=dict)
    application_integration_to_charm_integration: dict[
        tuple[frozenset[tuple[str, str]], frozenset[tuple[int, str]]], z3.BoolRef
    ] = Field(default_factory=dict)

    charms: list[ProblemSpaceCharm] = Field(default_factory=list)
    charm_integrations: dict[frozenset[tuple[int, str]], ProblemSpaceCharmIntegration] = Field(default_factory=dict)

    handled_failed_assertions: set[str] = Field(default_factory=set)


def initialize_problem_space(
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


def add_charm_to_problem_space(charm: Charm, problem_space: ProblemSpace) -> ProblemSpace:
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

    for other_charm_id, other_charm in enumerate(problem_space.charms):
        if other_charm_id == charm_id:
            continue
        for endpoint_name, endpoint in charm.endpoints.items():
            for other_endpoint_name, other_endpoint in other_charm.spec.endpoints.items():
                if endpoint.interface != other_endpoint.interface:
                    continue
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

    for application, constraints in problem_space.application_constraints.items():
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

    for integration in problem_space.integration_constraints:
        app_ep_1, app_ep_2 = sorted(integration)
        for charm_integration in problem_space.charm_integrations:
            charm_ep_1, charm_ep_2 = sorted(charm_integration)
            # Check endpoint names match
            if {app_ep_1[1], app_ep_2[1]} != {charm_ep_1[1], charm_ep_2[1]}:
                continue
            
            # Check that valid application-to-charm mappings exist for at least one orientation
            # Option A: app1 maps to charm1 AND app2 maps to charm2
            option_a_valid = (
                (app_ep_1[0], charm_ep_1[0]) in problem_space.application_to_charm
                and (app_ep_2[0], charm_ep_2[0]) in problem_space.application_to_charm
                and app_ep_1[1] == charm_ep_1[1]
                and app_ep_2[1] == charm_ep_2[1]
            )
            # Option B: app1 maps to charm2 AND app2 maps to charm1
            option_b_valid = (
                (app_ep_1[0], charm_ep_2[0]) in problem_space.application_to_charm
                and (app_ep_2[0], charm_ep_1[0]) in problem_space.application_to_charm
                and app_ep_1[1] == charm_ep_2[1]
                and app_ep_2[1] == charm_ep_1[1]
            )
            
            if not (option_a_valid or option_b_valid):
                continue
                
            problem_space.application_integration_to_charm_integration[(integration, charm_integration)] = z3.Bool(
                f"app_integration_{app_ep_1[0]}:{app_ep_1[1]}__{app_ep_2[0]}:{app_ep_2[1]}_maps_to_charm_integration_{charm_ep_1[0]}:{charm_ep_1[1]}__{charm_ep_2[0]}:{charm_ep_2[1]}"
            )

    return problem_space


def add_charms_for_endpoint(
    charm_id: int, endpoint_name: str, problem_space: ProblemSpace, charmhub_client: CharmhubClient
) -> ProblemSpace:
    endpoint = problem_space.charms[charm_id].spec.endpoints[endpoint_name]

    fulfilling_charms: set[str] = set()
    if endpoint.type == EndpointType.REQUIRES:
        fulfilling_charms = charmhub_client.find_charms(
            provides=endpoint.interface, platform=problem_space.platform_constraint
        )
    elif endpoint.type == EndpointType.PROVIDES:
        fulfilling_charms = charmhub_client.find_charms(
            requires=endpoint.interface, platform=problem_space.platform_constraint
        )

    if len(fulfilling_charms) == 0:
        charm_name = problem_space.charms[charm_id].spec.name
        raise ValueError(
            f"No charms found that expose interface '{endpoint.interface}' to satisfy {charm_name}:{endpoint_name}"
        )

    for charm in fulfilling_charms:
        spec = charmhub_client.charm_from_store(
            charm_name=charm,
            ubuntu_arch=problem_space.arch_constraint,
        )
        problem_space = add_charm_to_problem_space(spec, problem_space)

    return problem_space
