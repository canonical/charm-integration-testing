# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta


class JujuClient(ABC):
    @abstractmethod
    def scale_application(self):
        raise NotImplementedError

    @abstractmethod
    def num_units(self, application: str) -> int:
        raise NotImplementedError

    @abstractmethod
    def are_idle(self, *applications: list[str]) -> bool:
        raise NotImplementedError

    def wait_idle(
        self,
        *applications: list[str],
        timeout: timedelta = None,
        interval: timedelta = timedelta(seconds=1),
        threshold: timedelta = timedelta(seconds=3),
    ):
        # Check idle
        start = datetime.now()
        idle_for = -interval
        while start + timeout > datetime.now() if timeout else True:
            # Check for idle
            if self.are_idle(*applications):
                idle_for += interval
            else:
                idle_for = -interval

            # Check if idle above threshold
            if idle_for >= threshold:
                return

            # Wait
            time.sleep(interval.seconds)

        # Not idle
        raise TimeoutError("Applications did not reach idle")
