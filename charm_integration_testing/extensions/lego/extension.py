# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from abc import ABC

from juju import JujuBackend, JujuExtension

LEGO_CHARM = "lego"

# lego's `_validate_charm_config_options()` requires `plugin` to be non-empty and
# `plugin-config-secret-id` to point to an accessible secret before the charm can reach
# `active` (see canonical/lego-operator src/charm.py). We use the `httpreq` plugin because
# its config validator (`plugin_configs.httpreq.validate`) only checks that
# `HTTPREQ_ENDPOINT` is a syntactically valid HTTP(S) URL - it never dials the endpoint, and
# a failed certificate request is reported via relation error data, not `BlockedStatus`
# (see `_generate_signed_certificate`). So a dummy, unreachable endpoint is sufficient for
# the charm to settle into `active` for integration testing purposes.
HTTPREQ_ENDPOINT_CONFIG_KEY = "httpreq-endpoint"
DUMMY_HTTPREQ_ENDPOINT = "http://127.0.0.1:8080"


class LegoExtension(JujuExtension, ABC):
    juju: JujuBackend
    logger: logging.Logger

    def __init__(self, juju: JujuBackend, logger: logging.Logger) -> None:
        self.juju = juju
        self.logger = logger

    def post_deploy(self, model: str) -> None:
        # Look for lego applications
        for application in self.juju.list_applications(model):
            if self.juju.application_charm(model, application) == LEGO_CHARM:
                self.configure_lego(model, application)

    def configure_lego(self, model: str, application: str) -> None:
        # Skip if already configured (e.g. by the operator or a previous run)
        config = self.juju.get_application_config(model, application)
        if config.get("plugin-config-secret-id"):
            self.logger.info(f"Application '{application}' already has plugin-config-secret-id set, skipping")
            return

        self.logger.info(f"Configuring lego application '{application}' with dummy httpreq plugin config")

        # Create a secret carrying the (dummy) DNS provider endpoint and grant it to the
        # application, following the same add_secret/grant_secret pattern used by the
        # unseal_vault extension.
        secret_name = self.lego_plugin_config_secret_name(application)
        secret_id = self.juju.add_secret(model, secret_name, {HTTPREQ_ENDPOINT_CONFIG_KEY: DUMMY_HTTPREQ_ENDPOINT})
        self.juju.grant_secret(model, secret_name, application)

        # Point lego at the httpreq plugin and the secret we just created
        self.juju.configure_application(
            model,
            application,
            {
                "plugin": "httpreq",
                "plugin-config-secret-id": f"secret:{secret_id}",
            },
        )

    @staticmethod
    def lego_plugin_config_secret_name(application: str) -> str:
        return f"lego-secret-application-{application}-plugin-config"
