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

    def wait_idle(self, *applications: list[str], timeout: timedelta = None):
        # Let juju settle
        time.sleep(1)

        # Check idle
        start = datetime.now()
        while start + timeout > datetime.now() if timeout else True:
            if self.are_idle(*applications):
                return
            time.sleep(3)

        # Not idle
        raise TimeoutError("Applications did not reach idle")
