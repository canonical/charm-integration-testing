# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import secrets

from juju import JujuBackend, JujuExtension, JujuModelHandle

_SIGNING_SECRET_CONFIG = "signing-keys-secret-id"  # nosec B105


class SupersetSigningSecretExtension(JujuExtension):
    def __init__(self, juju: JujuBackend, logger: logging.Logger) -> None:
        self.juju = juju
        self.logger = logger.getChild(type(self).__name__)

    def post_deploy(self, model: JujuModelHandle) -> None:
        applications = [
            application
            for application, info in self.juju.list_applications(model).items()
            if info.charm == "superset-k8s"
        ]
        configs = {application: self.juju.get_application_config(model, application) for application in applications}
        missing = [
            application
            for application, config in configs.items()
            if _SIGNING_SECRET_CONFIG in config and not config[_SIGNING_SECRET_CONFIG]
        ]
        if not missing:
            return
        if len(applications) != 1:
            raise RuntimeError(
                f"Cannot determine the signing-secret scope for multiple Superset applications in {model.uri}; "
                "configure signing-keys-secret-id explicitly."
            )

        application = missing[0]
        self.logger.info("Provisioning a signing secret for %s in %s.", application, model.uri)
        secret_id = self.juju.add_secret(
            model,
            f"charmqa-{application}-signing-{secrets.token_hex(4)}",
            {"secret-key": secrets.token_urlsafe(48), "async-queries-jwt": secrets.token_urlsafe(48)},
        )
        self.juju.grant_secret(model, secret_id, application)
        self.juju.configure_application(model, application, {_SIGNING_SECRET_CONFIG: secret_id})
