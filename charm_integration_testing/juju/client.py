# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC, abstractmethod
from datetime import timedelta


class JujuClient(ABC):
    @abstractmethod
    def scale_application(self, application: str, num: int):
        raise NotImplementedError

    @abstractmethod
    def num_units(self, application: str) -> int:
        raise NotImplementedError

    @abstractmethod
    def wait_idle(self, model: str = "default", timeout: timedelta = timedelta(days=1)) -> bool:
        raise NotImplementedError
