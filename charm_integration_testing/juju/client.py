# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
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

    # Logging wrapper around wait idle
    def wait_idle(self, model: str = "default", timeout: timedelta = timedelta(days=1)):
        # Start logging
        self.logger.info("Waiting for idle.\n::group::Juju Status:")

        # Wait for idle
        try:
            # Loop until timeout
            end_time = datetime.now(timezone.utc) + timeout
            while datetime.now(timezone.utc) < end_time:
                try:
                    self.backend.wait_idle(model=model, timeout=timedelta(seconds=5))
                except JujuWaitIdleTimeoutError:
                    # Wait idle did not conclude, try again
                    pass
                else:
                    # Process is idle
                    return
                finally:
                    # Always print status
                    self.print_status()
        finally:
            # Always end group log
            self.logger.info("End waiting for idle.\n::endgroup::")

        raise JujuWaitIdleTimeoutError

    def print_status(self):
        (self.logger.info(f"Juju Status:\n{self.backend.juju_status_text()}"),)
