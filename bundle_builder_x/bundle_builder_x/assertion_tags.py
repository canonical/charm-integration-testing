# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import json
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .domain import ModelRef


class Assertions(str, Enum):
    APPLICATION_EXISTS = "application_exists"
    APPLICATION_INTEGRATION_EXISTS = "application_integration_exists"
    CHARM_DEPENDENCY_CYCLIC = "charm_dependency_cyclic"
    CHARM_ENDPOINT_NON_OPTIONAL = "charm_endpoint_non_optional"
    CHARM_MAPPED_TO_SINGLE_APPLICATION = "charm_mapped_to_single_application"
    CHARM_INTEGRATION_MAPPED_TO_SINGLE_APPLICATION_INTEGRATION = (
        "charm_integration_mapped_to_single_application_integration"
    )
    CHARM_EXISTS_FROM_APPLICATION = "charm_exists_from_application"
    CHARM_INTEGRATION_EXISTS_FROM_APPLICATION_INTEGRATION = "charm_integration_exists_from_application_integration"
    CHARM_EXISTS_FROM_INTEGRATION = "charm_exists_from_integration"
    ENDPOINT_COUNT_MATCHES_INTEGRATIONS = "endpoint_count_matches_integrations"
    ENDPOINT_INTEGRATED_MATCHES_COUNT = "endpoint_integrated_matches_count"
    ENDPOINT_RESPECTS_LIMIT = "endpoint_respects_limit"
    APPLICATION_INTEGRATION_APPS_MAP_TO_CHARMS = "application_integration_apps_map_to_charms"
    CHARM_CUSTOM_CONSTRAINT = "charm_custom_constraint"
    PEER_CHANNEL_MISMATCH = "peer_channel_mismatch"
    CHARM_CONFIG_INDEX_IN_RANGE = "charm_config_index_in_range"
    CHARM_CONFIG_VALUE_MATCHES_INDEX = "charm_config_value_matches_index"
    CHARM_RANK_BOUNDED = "charm_rank_bounded"
    SUBORDINATE_BASE_MISMATCH = "subordinate_base_mismatch"


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
    model: ModelRef = Field(default_factory=ModelRef)


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
    model: ModelRef
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
    model: ModelRef
    integration: list[AppEndpointPayload]


class CharmDependencyCyclicTag(AssertionTag):
    kind: Assertions = Assertions.CHARM_DEPENDENCY_CYCLIC
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
    application_integration: list[AppEndpointPayload]
    charm_integration: list[CharmEndpointPayload]


class CharmExistsFromIntegrationTag(AssertionTag):
    kind: Assertions = Assertions.CHARM_EXISTS_FROM_INTEGRATION
    charm: CharmPayload
    integration: list[CharmEndpointPayload]


class EndpointCountMatchesIntegrationsTag(AssertionTag):
    kind: Assertions = Assertions.ENDPOINT_COUNT_MATCHES_INTEGRATIONS
    charm: CharmEndpointPayload
    num_terms: int


class EndpointIntegratedMatchesCountTag(AssertionTag):
    kind: Assertions = Assertions.ENDPOINT_INTEGRATED_MATCHES_COUNT
    charm: CharmEndpointPayload


class CharmEndpointNonOptionalTag(AssertionTag):
    kind: Assertions = Assertions.CHARM_ENDPOINT_NON_OPTIONAL
    charm: CharmEndpointPayload
    interface: str | None = None


class EndpointRespectsLimitTag(AssertionTag):
    kind: Assertions = Assertions.ENDPOINT_RESPECTS_LIMIT
    charm: CharmEndpointPayload
    limit: int


class CharmCustomConstraintTag(AssertionTag):
    kind: Assertions = Assertions.CHARM_CUSTOM_CONSTRAINT
    charm: CharmPayload
    assertion_idx: int


class PeerChannelMismatchTag(AssertionTag):
    kind: Assertions = Assertions.PEER_CHANNEL_MISMATCH
    charm: CharmPayload
    endpoint: str
    peer_charm_name: str
    peer_charm_id: int
    required_track: str | None = None
    required_risk: str | None = None
    required_channel: str | None = None
    required_revision: int | None = None


class CharmConfigIndexInRangeTag(AssertionTag):
    kind: Assertions = Assertions.CHARM_CONFIG_INDEX_IN_RANGE
    charm: CharmPayload


class CharmConfigValueMatchesIndexTag(AssertionTag):
    kind: Assertions = Assertions.CHARM_CONFIG_VALUE_MATCHES_INDEX
    charm: CharmPayload
    config_key: str
    config_index: int


class CharmRankBoundedTag(AssertionTag):
    kind: Assertions = Assertions.CHARM_RANK_BOUNDED
    charm: CharmPayload


class SubordinateBaseMismatchTag(AssertionTag):
    kind: Assertions = Assertions.SUBORDINATE_BASE_MISMATCH
    subordinate_charm_name: str
    subordinate_charm_id: int
    subordinate_endpoint: str
    principal_charm_name: str
    principal_charm_id: int
    principal_endpoint: str
    subordinate_base: str
    principal_base: str


_ASSERTION_TYPE_REGISTRY: dict[Assertions, type[AssertionTag]] = {
    Assertions.APPLICATION_EXISTS: ApplicationExistsTag,
    Assertions.APPLICATION_INTEGRATION_EXISTS: ApplicationIntegrationExistsTag,
    Assertions.CHARM_DEPENDENCY_CYCLIC: CharmDependencyCyclicTag,
    Assertions.CHARM_ENDPOINT_NON_OPTIONAL: CharmEndpointNonOptionalTag,
    Assertions.CHARM_MAPPED_TO_SINGLE_APPLICATION: CharmMappedToSingleApplicationTag,
    Assertions.CHARM_INTEGRATION_MAPPED_TO_SINGLE_APPLICATION_INTEGRATION: CharmIntegrationMappedToSingleApplicationIntegrationTag,
    Assertions.CHARM_EXISTS_FROM_APPLICATION: CharmExistsFromApplicationTag,
    Assertions.CHARM_INTEGRATION_EXISTS_FROM_APPLICATION_INTEGRATION: CharmIntegrationExistsFromApplicationIntegrationTag,
    Assertions.CHARM_EXISTS_FROM_INTEGRATION: CharmExistsFromIntegrationTag,
    Assertions.ENDPOINT_COUNT_MATCHES_INTEGRATIONS: EndpointCountMatchesIntegrationsTag,
    Assertions.ENDPOINT_INTEGRATED_MATCHES_COUNT: EndpointIntegratedMatchesCountTag,
    Assertions.ENDPOINT_RESPECTS_LIMIT: EndpointRespectsLimitTag,
    Assertions.APPLICATION_INTEGRATION_APPS_MAP_TO_CHARMS: ApplicationIntegrationAppsMapToCharmsTag,
    Assertions.CHARM_CUSTOM_CONSTRAINT: CharmCustomConstraintTag,
    Assertions.PEER_CHANNEL_MISMATCH: PeerChannelMismatchTag,
    Assertions.CHARM_CONFIG_INDEX_IN_RANGE: CharmConfigIndexInRangeTag,
    Assertions.CHARM_CONFIG_VALUE_MATCHES_INDEX: CharmConfigValueMatchesIndexTag,
    Assertions.CHARM_RANK_BOUNDED: CharmRankBoundedTag,
    Assertions.SUBORDINATE_BASE_MISMATCH: SubordinateBaseMismatchTag,
}
