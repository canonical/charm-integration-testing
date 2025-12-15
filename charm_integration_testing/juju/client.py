# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import timedelta

from .backend import JujuBackend, JujuIntegrationApplication
from .extension import JujuExtension


class JujuClient:
    backend: JujuBackend
    logger: logging.Logger
    extensions: list[JujuExtension]

    def __init__(self, backend: JujuBackend, logger: logging.Logger, extensions: list[JujuExtension] | None = None):
        self.backend = backend
        self.logger = logger
        self.extensions = extensions or []

    def scale_application(self, application: str, num: int, model: str = "default"):
        self.logger.info(f"Scaling application {application} to {num} units.")
        self.backend.scale_application(model, application, num)

        # Call extensions
        for extension in self.extensions:
            extension.post_scale(model)

    def num_units(self, application: str, model: str = "default") -> int:
        self.logger.info(f"Getting the number of units for {application}.")
        return self.backend.num_units(model, application)

    @staticmethod
    def _waiting_timeout_log(timeout: timedelta | None) -> str:
        if timeout is not None:
            return f"Waiting {timeout}"
        else:
            return "Waiting"

    # Wait for the Juju model to become idle
    def idle_for_period(
        self,
        model: str = "default",
        timeout: timedelta | None = None,
        count: int = 30,
        strict_timeout: bool = False,
    ):
        self.logger.info(f"{self._waiting_timeout_log(timeout)} to be idle.")
        self.backend.wait_idle(model=model, timeout=timeout, count=count, strict_timeout=strict_timeout)

    def print_status(self, model: str = "default"):
        separator = "-" * 80
        self.logger.info(f"Juju Status:\n{separator}\n{self.backend.juju_status_text(model)}{separator}")

    def integrate(
        self,
        application_1: str,
        application_2: str,
        endpoint_1: str,
        endpoint_2: str,
        model: str = "default",
    ):
        # Get targets
        target_1 = JujuIntegrationApplication(application_1, endpoint_1)
        target_2 = JujuIntegrationApplication(application_2, endpoint_2)

        # Integrate
        self.logger.info(f"Integrating {target_1} with {target_2}.")
        self.backend.integrate(model, target_1, target_2)

    def remove_integration(
        self,
        application_1: str,
        application_2: str,
        endpoint_1: str,
        endpoint_2: str,
        model: str = "default",
    ):
        # Get targets
        target_1 = JujuIntegrationApplication(application_1, endpoint_1)
        target_2 = JujuIntegrationApplication(application_2, endpoint_2)

        # Remove integration
        self.logger.info(f"Removing integration between {target_1} and {target_2}.")
        self.backend.remove_integration(model, target_1, target_2)

    def deploy_bundle_file(
        self,
        bundle: str,
        model: str = "default",
    ):
        self.logger.info(f"Deploying bundle file: '{bundle}'")
        self.backend.deploy_bundle_file(model, bundle)

        # Call extensions
        for extension in self.extensions:
            extension.post_deploy(model)

    def remove_applications(self, *applications: str, model: str = "default"):
        self.logger.info(f"Removing applications: {', '.join(applications)}.")
        self.backend.remove_applications(model, *applications)

    def wait_for_removal(self, *applications: str, model: str = "default", timeout: timedelta | None = None):
        self.logger.info(
            f"{self._waiting_timeout_log(timeout)} for removal of application(s) {', '.join(applications)}."
        )
        self.backend.wait_for_removal(model, applications, timeout)

    def wait_for_removal_of_integration(
        self,
        application_1: str,
        application_2: str,
        endpoint_1: str,
        endpoint_2: str,
        model: str = "default",
        timeout: timedelta | None = None,
    ):
        target_1 = JujuIntegrationApplication(application_1, endpoint_1)
        target_2 = JujuIntegrationApplication(application_2, endpoint_2)
        self.logger.info(
            f"{self._waiting_timeout_log(timeout)} for removal of integration between {target_1} and {target_2}."
        )
        self.backend.wait_for_removal_of_integration(model, target_1, target_2, timeout)

    def wait_for_removal_of_units(self, *applications: str, model: str = "default", timeout: timedelta | None = None):
        self.logger.info(
            f"{self._waiting_timeout_log(timeout)} for removal of all units of application(s) {', '.join(applications)}."
        )
        self.backend.wait_for_removal_of_units(model, applications, timeout)

    def application_exists(self, application: str, model: str = "default") -> bool:
        self.logger.info(f"Checking that application exists: {application}.")
        return application in self.backend.list_applications(model)

    def integration_exists(
        self, application_1: str, endpoint_1: str, application_2: str, endpoint_2: str, model: str = "default"
    ) -> bool:
        self.logger.info(
            f"Checking that integration exists: {application_1}:{endpoint_1}/{application_2}:{endpoint_2}."
        )
        return {
            JujuIntegrationApplication(application_1, endpoint_1),
            JujuIntegrationApplication(application_2, endpoint_2),
        } in {integration.applications for integration in self.backend.list_integrations(model)}

    def get_charm_revisions(self, model: str = "default") -> set[tuple[str, int]]:
        return self.backend.get_charm_revisions(model)
