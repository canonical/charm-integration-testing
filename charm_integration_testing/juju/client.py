# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import time
from datetime import datetime, timedelta, timezone

from .backend import JujuBackend, JujuWaitIdleTimeoutError


class JujuClient:
    backend: JujuBackend
    logger: logging.Logger

    def __init__(self, backend, logger: logging.Logger):
        self.backend = backend
        self.logger = logger

    def scale_application(self, application: str, num: int):
        self.logger.info(f"Scaling application {application} to {num} units.")
        self.backend.scale_application(application, num)

    def num_units(self, application: str) -> int:
        self.logger.info(f"Getting the number of units for {application}.")
        return self.backend.num_units(application)

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
            self.print_status()

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
            except JujuWaitIdleTimeoutError:
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
        raise JujuWaitIdleTimeoutError

    def print_status(self):
        (self.logger.info(f"Juju Status:\n{self.backend.juju_status_text()}"),)
