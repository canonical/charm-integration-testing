# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

from charm_integration_testing.serializeable_dataclass import serializeable_dataclass


@serializeable_dataclass
class CharmDeployment:
    charm: str
    application_name: str = None
    model: str = None
    revision: int = None
    channel: str = None
    base: str = None


@serializeable_dataclass
class CharmIntegration:
    application_a: str
    application_b: str
    endpoint_a: str = None
    endpoint_b: str = None
