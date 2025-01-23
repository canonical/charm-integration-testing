# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC, abstractmethod
from datetime import timedelta


class JujuWaitIdleTimeoutError(TimeoutError):
    def __init__(self, message="Never reached idle state"):
        super().__init__(message)


class JujuBackend(ABC):
    @abstractmethod
    def scale_application(self, model: str, application: str, num: int):
        raise NotImplementedError

    @abstractmethod
    def num_units(self, application: str) -> int:
        raise NotImplementedError

    @abstractmethod
    def wait_idle(self, model: str, timeout: timedelta):
        raise NotImplementedError

    @abstractmethod
    def juju_status_text(self) -> str:
        raise NotImplementedError
