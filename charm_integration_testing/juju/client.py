# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import datetime, timedelta, timezone
import time

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
    def idle_for_period(self, model: str = "default", timeout: timedelta = timedelta(days=1), idle_period: timedelta = timedelta(seconds=15)):
        # Start logging
        self.logger.info("Waiting for idle.\n::group::Juju Status:")

        # Wait for idle
        try:
            self._idle_for_period(model, timeout, idle_period)
        finally:
            # Always end group log
            self.logger.info("End waiting for idle.\n::endgroup::")
    
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
                idle_since = None
                self.print_status()
            else:
                # Model is idle
                if idle_since is None:
                    # Model is idle for the first time
                    self.print_status()
                    self.logger.info("Applications are idle, checking for idle period.")
                    idle_since = datetime.now(timezone.utc)
                elif datetime.now(timezone.utc) - idle_since > idle_period:
                    # Model is still idle and idle_period is met
                    self.logger.info("Applications are now idle.")
                    return
                else:
                    # Model is still idle but idle_period not met
                    self.logger.info("Applications are still idle.")
                
                # Wait before checking again
                time.sleep(1)
        
        raise JujuWaitIdleTimeoutError

    def print_status(self):
        (self.logger.info(f"Juju Status:\n{self.backend.juju_status_text()}"),)
