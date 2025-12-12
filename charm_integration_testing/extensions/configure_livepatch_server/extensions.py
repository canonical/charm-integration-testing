# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from abc import ABC
from datetime import timedelta

from juju import JujuBackend, JujuExtension

LIVEPATCH_SERVER_CHARM = "canonical-livepatch-server-k8s"
LIVEPATCH_SERVER_CONFIGURE_MESSAGE = "patch-sync token not set"


class ConfigureLivepatchServerExtension(JujuExtension, ABC):
    juju: JujuBackend
    logger: logging.Logger
    ubuntu_pro_token: str | None

    def __init__(self, juju: JujuBackend, logger: logging.Logger, ubuntu_pro_token: str | None):
        self.juju = juju
        self.logger = logger
        self.ubuntu_pro_token = ubuntu_pro_token

    def post_deploy(self, model: str):
        # Look for livepatch server application
        for application in self.juju.list_applications(model):
            if self.juju.application_charm(model, application) == LIVEPATCH_SERVER_CHARM:
                self.configure_livepatch_server(model, application)

    def configure_livepatch_server(self, model: str, application: str):
        # Following guide: https://discourse.ubuntu.com/t/getting-started-with-livepatch-on-prem-and-microk8s/39130

        # Skip if no token
        if self.ubuntu_pro_token is None:
            self.logger.warning(
                f"No Ubuntu Pro token provided, skipping livepatch server configuration for application '{application}'"
            )
            return
        self.logger.info(f"Configuring livepatch server application '{application}'")

        # Wait for application to be scaled
        self.logger.info(f"Waiting for application '{application}' to be scaled")
        self.juju.wait_application_scaled(model, application, timedelta(minutes=10))

        # Wait for application to be settled
        self.logger.info(f"Waiting for application '{application}' to be settled")
        self.juju.wait_application_settled(model, application, timedelta(minutes=10))

        # Wait for message
        self.logger.info(f"Waiting for application '{application}' to ask for token configuration")
        self.juju.wait_for_unit_message(model, f"{application}/leader", LIVEPATCH_SERVER_CONFIGURE_MESSAGE, timedelta(minutes=10))

        # Configure ubuntu pro token
        self.juju.run_action(
            model,
            f"{application}/leader",
            "get-resource-token",
            {"contract-token": self.ubuntu_pro_token},
        )

        # Get unit ip
        unit_ip = self.juju.unit_ip(model, f"{application}/leader")

        # Configure server URL template
        self.juju.configure_application(
            model,
            application,
            {
                "server.url-template": f"http://{unit_ip}:8080/v1/patches/{{filename}}",
            },
        )
