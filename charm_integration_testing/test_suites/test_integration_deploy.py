# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.


from charm_integration_testing.juju import CharmDeployment, JujuClient, CharmIntegration
from charm_integration_testing.serializeable_dataclass import serializeable_dataclass


@serializeable_dataclass
class IntegrationDeployParams:
    charm: CharmDeployment
    neighbor: CharmDeployment
    integration: CharmIntegration


def test_integration_deploy(juju_client: JujuClient, test_params: IntegrationDeployParams):
    juju_client.deploy(test_params.charm)
    juju_client.deploy(test_params.neighbor)
    juju_client.integrate(integration)
