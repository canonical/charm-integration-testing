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

    def _wait_for(self, function, description: str, timeout: timedelta):
        # Start logging
        self.logger.info(f"Begin waiting for {description}.\n::group::Wait for {description}.")

        # Wait for
        try:
            # Loop until timeout
            start_time = datetime.now(timezone.utc)
            while datetime.now(timezone.utc) < start_time + timeout:
                try:
                    function(timedelta(seconds=1))
                except JujuWaitTimeoutError:
                    self.logger.info("Still waiting.")
                else:
                    self.logger.info("Finished waiting.")
                    return

            # Timed out
            raise JujuWaitTimeoutError
        finally:
            # End the log group
            self.logger.info("Stopped waiting for, ending log group.\n::endgroup::")

    def _wait_for_period(
        self,
        function,
        description: str,
        timeout: timedelta,
        period: timedelta,
    ):
        # Start logging
        self.logger.info(f"Begin waiting for period of {description}.\n::group::Wait for period of {description}.")

        # Wait for period
        try:
            # Loop until timeout
            start = datetime.now(timezone.utc)
            since = None
            while datetime.now(timezone.utc) < start + timeout:
                try:
                    function(timedelta(seconds=1))
                except JujuWaitTimeoutError:
                    # Wait condition not met
                    if since is not None:
                        since = None
                        self.logger.info("Wait condition no longer met")
                    else:
                        self.logger.info("Still waiting")
                else:
                    # Wait condition met
                    if since is None:
                        # First time wait condition is met
                        self.logger.info("Wait is condition met.")
                        since = datetime.now(timezone.utc)
                    elif datetime.now(timezone.utc) - since > period:
                        # Wait condition and period have been met
                        self.logger.info("Wait condition has been met for period.")
                        return
                    else:
                        # Wait condition met but period not met
                        self.logger.info("Wait condition still met.")

                    # Wait before checking again
                    time.sleep(1)

            # Timed out
            raise JujuWaitTimeoutError
        finally:
            # End the log group
            self.logger.info("Stopped waiting for period, ending log group.\n::endgroup::")

    # Wait for the Juju model to become idle
    def idle_for_period(
        self,
        model: str = "default",
        timeout: timedelta = timedelta(days=1),
        idle_period: timedelta = timedelta(seconds=15),
    ):
        self._wait_for_period(
            lambda log_timeout: self.backend.wait_idle(model, log_timeout),
            "model to be idle",
            timeout=timeout,
            period=idle_period,
        )

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
        target_1 = self._format_endpoint(application_1, endpoint_1)
        target_2 = self._format_endpoint(application_2, endpoint_2)

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
        target_1 = self._format_endpoint(application_1, endpoint_1)
        target_2 = self._format_endpoint(application_2, endpoint_2)

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
        self._wait_for(
            lambda log_timeout: self.backend.wait_for_removal(model, applications, log_timeout),
            f"removal of applications: {', '.join(applications)}",
            timeout,
        )

    def wait_for_removal_of_integration(
        self,
        application_1: str,
        application_2: str,
        endpoint_1: str,
        endpoint_2: str,
        model: str = "default",
        timeout: timedelta = timedelta(days=1),
    ):
        target_1 = JujuIntegrationApplication(application_1, endpoint_1)
        target_2 = JujuIntegrationApplication(application_2, endpoint_2)
        self._wait_for(
            lambda log_timeout: self.backend.wait_for_removal_of_integration(model, target_1, target_2, log_timeout),
            f"removal of integration: {target_1} <-> {target_2}",
            timeout,
        )

    def wait_for_removal_of_units(
        self, *applications: str, model: str = "default", timeout: timedelta = timedelta(days=1)
    ):
        self._wait_for(
            lambda log_timeout: self.backend.wait_for_removal_of_units(model, applications, log_timeout),
            f"removal of all units of applications: {', '.join(applications)}",
            timeout,
        )

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
