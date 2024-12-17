# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.


from charm_integration_testing.juju import CharmDeployment, JujuClient, CharmIntegration

from .cmd import CmdArg, CmdClient


class JujuCmdClient(JujuClient):
    cmd_client: CmdClient

    def __init__(self, cmd_client: CmdClient = None):
        self.cmd_client = cmd_client if cmd_client is not None else CmdClient()

    def call_juju(self, *args: list[CmdArg]):
        self.cmd_client.call(CmdArg("juju"), *args)

    def deploy(self, deployment: CharmDeployment):
        self.call_juju(
            CmdArg("deploy"),
            CmdArg(deployment.charm),
            CmdArg(deployment.application_name),
            CmdArg(deployment.model, name="model"),
            CmdArg(deployment.revision, name="revision"),
            CmdArg(deployment.channel, name="channel"),
            CmdArg(deployment.base, name="base"),
        )

    @staticmethod
    def get_integration_endpoint(application: str, endpoint: str | None) -> str:
        return f"{application}:{endpoint}" if endpoint is not None else application

    def integrate(self, integration: CharmIntegration):
        self.call_juju(
            CmdArg("integrate"),
            CmdArg(self.get_integration_endpoint(integration.application_a, integration.endpoint_a)),
            CmdArg(self.get_integration_endpoint(integration.application_b, integration.endpoint_b)),
        )
