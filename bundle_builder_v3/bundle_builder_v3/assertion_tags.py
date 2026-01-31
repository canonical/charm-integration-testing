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

import json
from enum import Enum
from typing import Type

from pydantic import BaseModel, ConfigDict


class Assertions(str, Enum):
    APPLICATION_EXISTS = "application_exists"
    APPLICATION_INTEGRATION_EXISTS = "application_integration_exists"
    CHARM_DEPENDENCY_ACYCLIC = "charm_dependency_acyclic"
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
    APPLICATION_INTEGRATION_APPS_MAP_TO_CHARMS = "application_integration_apps_map_to_charms"
    CHARM_CUSTOM_CONSTRAINT = "charm_custom_constraint"


class AssertionTag(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Assertions

    def encode(self) -> str:
        payload = self.model_dump(exclude={"kind"}, exclude_none=True)
        payload_json = json.dumps(payload, separators=(",", ":"))
        return f"{self.kind.value}::{payload_json}"

    @classmethod
    def decode(cls, tag: str) -> "AssertionTag":
        if "::" not in tag:
            raise ValueError(f"Invalid assertion tag: {tag}")
        kind_str, payload_json = tag.split("::", 1)
        kind = Assertions(kind_str)
        subtype = _ASSERTION_TYPE_REGISTRY.get(kind)
        if subtype is None:
            raise ValueError(f"Unknown assertion kind: {kind_str}")
        payload = json.loads(payload_json) if payload_json else {}
        return subtype(kind=kind, **payload)


class AppEndpointPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    application: str
    endpoint: str


class CharmPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    charm_name: str
    charm_id: int


class CharmEndpointPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    charm_name: str
    charm_id: int
    endpoint: str


class ApplicationExistsTag(AssertionTag):
    kind: Assertions = Assertions.APPLICATION_EXISTS
    application: str


class CharmMappedToSingleApplicationTag(AssertionTag):
    kind: Assertions = Assertions.CHARM_MAPPED_TO_SINGLE_APPLICATION
    charm: CharmPayload


class CharmExistsFromApplicationTag(AssertionTag):
    kind: Assertions = Assertions.CHARM_EXISTS_FROM_APPLICATION
    application: str
    charm: CharmPayload


class ApplicationIntegrationExistsTag(AssertionTag):
    kind: Assertions = Assertions.APPLICATION_INTEGRATION_EXISTS
    integration: list[AppEndpointPayload]


class CharmDependencyAcyclicTag(AssertionTag):
    kind: Assertions = Assertions.CHARM_DEPENDENCY_ACYCLIC
    requiring_charm: CharmEndpointPayload
    providing_charm: CharmEndpointPayload


class CharmIntegrationMappedToSingleApplicationIntegrationTag(AssertionTag):
    kind: Assertions = Assertions.CHARM_INTEGRATION_MAPPED_TO_SINGLE_APPLICATION_INTEGRATION
    charm_integration: list[CharmEndpointPayload]


class CharmIntegrationExistsFromApplicationIntegrationTag(AssertionTag):
    kind: Assertions = Assertions.CHARM_INTEGRATION_EXISTS_FROM_APPLICATION_INTEGRATION
    application_integration: list[AppEndpointPayload]
    charm_integration: list[CharmEndpointPayload]


class ApplicationIntegrationAppsMapToCharmsTag(AssertionTag):
    kind: Assertions = Assertions.APPLICATION_INTEGRATION_APPS_MAP_TO_CHARMS
    application: str
    application_endpoint: str
    charm: CharmEndpointPayload
    application_integration: list[AppEndpointPayload]
    charm_integration: list[CharmEndpointPayload]


class CharmExistsFromIntegrationTag(AssertionTag):
    kind: Assertions = Assertions.CHARM_EXISTS_FROM_INTEGRATION
    charm: CharmPayload
    integration: list[CharmEndpointPayload]


class EndpointCountMatchesIntegrationsTag(AssertionTag):
    kind: Assertions = Assertions.ENDPOINT_COUNT_MATCHES_INTEGRATIONS
    charm: CharmEndpointPayload
    usage_count: int


class CharmEndpointNonOptionalTag(AssertionTag):
    kind: Assertions = Assertions.CHARM_ENDPOINT_NON_OPTIONAL
    charm: CharmEndpointPayload


class EndpointRespectsLimitTag(AssertionTag):
    kind: Assertions = Assertions.ENDPOINT_RESPECTS_LIMIT
    charm: CharmEndpointPayload
    limit: int


class CharmCustomConstraintTag(AssertionTag):
    kind: Assertions = Assertions.CHARM_CUSTOM_CONSTRAINT
    charm: CharmPayload
    assertion_idx: int


_ASSERTION_TYPE_REGISTRY: dict[Assertions, Type[AssertionTag]] = {
    Assertions.APPLICATION_EXISTS: ApplicationExistsTag,
    Assertions.APPLICATION_INTEGRATION_EXISTS: ApplicationIntegrationExistsTag,
    Assertions.CHARM_DEPENDENCY_ACYCLIC: CharmDependencyAcyclicTag,
    Assertions.CHARM_ENDPOINT_NON_OPTIONAL: CharmEndpointNonOptionalTag,
    Assertions.CHARM_MAPPED_TO_SINGLE_APPLICATION: CharmMappedToSingleApplicationTag,
    Assertions.CHARM_INTEGRATION_MAPPED_TO_SINGLE_APPLICATION_INTEGRATION: CharmIntegrationMappedToSingleApplicationIntegrationTag,
    Assertions.CHARM_EXISTS_FROM_APPLICATION: CharmExistsFromApplicationTag,
    Assertions.CHARM_INTEGRATION_EXISTS_FROM_APPLICATION_INTEGRATION: CharmIntegrationExistsFromApplicationIntegrationTag,
    Assertions.CHARM_EXISTS_FROM_INTEGRATION: CharmExistsFromIntegrationTag,
    Assertions.ENDPOINT_COUNT_MATCHES_INTEGRATIONS: EndpointCountMatchesIntegrationsTag,
    Assertions.ENDPOINT_RESPECTS_LIMIT: EndpointRespectsLimitTag,
    Assertions.APPLICATION_INTEGRATION_APPS_MAP_TO_CHARMS: ApplicationIntegrationAppsMapToCharmsTag,
    Assertions.CHARM_CUSTOM_CONSTRAINT: CharmCustomConstraintTag,
}
