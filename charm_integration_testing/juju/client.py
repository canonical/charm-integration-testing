# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import time
from datetime import datetime, timedelta, timezone

from .backend import JujuBackend, JujuIntegrationApplication, JujuWaitTimeoutError


class JujuClient:
    backend: JujuBackend
    logger: logging.Logger

    def __init__(self, backend: JujuBackend, logger: logging.Logger):
        self.backend = backend
        self.logger = logger

    def scale_application(self, application: str, num: int, model: str = "default"):
        self.logger.info(f"Scaling application {application} to {num} units.")
        self.backend.scale_application(model, application, num)

    def num_units(self, application: str, model: str = "default") -> int:
        self.logger.info(f"Getting the number of units for {application}.")
        return self.backend.num_units(model, application)

    # Wait for the Juju model to become idle
    def idle_for_period(
        self,
        model: str = "default",
        timeout: timedelta = timedelta(days=1),
        idle_period: timedelta = timedelta(seconds=15),
    ):
        # Start logging
        self.logger.info("Begin waiting for idle.\n::group::Wait for idle.")

        # Wait for idle
        try:
            self._idle_for_period(model, timeout, idle_period)
        finally:
            # Always print status at end
            self.print_status(model=model)

            # End the log group
            self.logger.info("Reached end of waiting for idle.\n::endgroup::")

    # Ensure the Juju model is idle for the given period
    def _idle_for_period(self, model: str, timeout: timedelta, idle_period: timedelta):
        # Loop until timeout
        start_time = datetime.now(timezone.utc)
        idle_since = None
        while datetime.now(timezone.utc) < start_time + timeout:
            try:
                self.backend.wait_idle(model=model, timeout=timedelta(seconds=1))
            except JujuWaitTimeoutError:
                # Model not idle, try again
                if idle_since is not None:
                    idle_since = None
                    self.logger.info("Model is no longer idle")
                else:
                    self.logger.info("Model is not idle")
            else:
                # Model is idle
                if idle_since is None:
                    # Model is idle for the first time
                    self.logger.info("Model is idle, checking for idle period.")
                    idle_since = datetime.now(timezone.utc)
                elif datetime.now(timezone.utc) - idle_since > idle_period:
                    # Model is still idle and idle_period is met
                    self.logger.info("Model has been idle for idle period.")
                    return
                else:
                    # Model is still idle but idle_period not met
                    self.logger.info("Model is still idle.")

                # Wait before checking again
                time.sleep(1)

        # Timed out
        self.logger.error("Model did not reach idle.")
        raise JujuWaitTimeoutError

    def print_status(self, model: str = "default"):
        (self.logger.info(f"Juju Status:\n{self.backend.juju_status_text(model)}"),)

    def _format_endpoint(self, application: str, endpoint: str) -> str:
        return f"{application}:{endpoint}"

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
        self.logger.info(f"Integrating {target_1} with {target_2}")
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
        self.logger.info(f"Removing integration between {target_1} and {target_2}")
        self.backend.remove_integration(model, target_1, target_2)

    def deploy_bundle_file(
        self,
        bundle: str,
        model: str = "default",
    ):
        self.logger.info(f"Deploying bundle file: '{bundle}'")
        self.backend.deploy_bundle_file(model, bundle)

    def remove_applications(self, *applications: str, model: str = "default"):
        self.logger.info(f"Removing applications: {', '.join(applications)}.")
        self.backend.remove_applications(model, *applications)

    def wait_for_removal(self, *applications: str, model: str = "default", timeout: timedelta = timedelta(days=1)):
        # Start logging
        self.logger.info(f"Begin waiting for removal of: {', '.join(applications)}.\n::group::Wait for removal.")

        # Wait for removal
        try:
            # Loop until timeout
            start_time = datetime.now(timezone.utc)
            while datetime.now(timezone.utc) < start_time + timeout:
                try:
                    self.backend.wait_for_removal(model, applications, timedelta(seconds=1))
                except JujuWaitTimeoutError:
                    # Applications still exist, try again
                    self.logger.info("Applications not removed.")
                else:
                    self.logger.info("Applications have been removed.")
                    return
        finally:
            # Always print status at end
            self.print_status(model=model)

            # End the log group
            self.logger.info("Reached end of waiting for removal.\n::endgroup::")

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
