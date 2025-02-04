# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC, abstractmethod
from datetime import timedelta


class JujuWaitTimeoutError(TimeoutError):
    def __init__(self, message="Timed out while waiting"):
        super().__init__(message)


class JujuBackend(ABC):
    @abstractmethod
    def scale_application(self, model: str, application: str, num: int):
        raise NotImplementedError

    @abstractmethod
    def num_units(self, model: str, application: str) -> int:
        raise NotImplementedError

    @abstractmethod
    def wait_idle(self, model: str, timeout: timedelta):
        raise NotImplementedError

    @abstractmethod
    def juju_status_text(self, model: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def integrate(self, model: str, target_1: str, target_2: str):
        raise NotImplementedError

    @abstractmethod
    def remove_integration(self, model: str, target_1: str, target_2: str):
        raise NotImplementedError

    @abstractmethod
    def deploy_bundle_file(self, model: str, bundle: str):
        raise NotImplementedError

    @abstractmethod
    def remove_applications(self, model: str, *applications: str):
        raise NotImplementedError

    @abstractmethod
    def wait_for_removal(self, model: str, applications: list[str], timeout: timedelta):
        raise NotImplementedError
