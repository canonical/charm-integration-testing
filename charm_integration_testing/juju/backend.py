# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

from abc import ABC, abstractmethod
from datetime import timedelta

from pydantic.dataclasses import dataclass


class JujuWaitTimeoutError(TimeoutError):
    def __init__(self, message="Timed out while waiting"):
        super().__init__(message)


@dataclass(frozen=True)
class JujuIntegrationApplication:
    application: str
    endpoint: str

    def __str__(self) -> str:
        return f"{self.application}:{self.endpoint}"


@dataclass(frozen=True)
class JujuIntegration:
    interface: str
    applications: frozenset[JujuIntegrationApplication]


class JujuBackend(ABC):
    @abstractmethod
    def scale_application(self, model: str, application: str, num: int):
        raise NotImplementedError

    @abstractmethod
    def num_units(self, model: str, application: str) -> int:
        raise NotImplementedError

    @abstractmethod
    def list_applications(self, model: str) -> set[str]:
        raise NotImplementedError

    @abstractmethod
    def list_integrations(self, model: str) -> set[JujuIntegration]:
        raise NotImplementedError

    @abstractmethod
    def wait_idle(self, model: str, timeout: timedelta):
        raise NotImplementedError

    @abstractmethod
    def juju_status_text(self, model: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def integrate(self, model: str, target_1: JujuIntegrationApplication, target_2: JujuIntegrationApplication):
        raise NotImplementedError

    @abstractmethod
    def remove_integration(
        self, model: str, target_1: JujuIntegrationApplication, target_2: JujuIntegrationApplication
    ):
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

    @abstractmethod
    def wait_for_removal_of_integration(
        self,
        model: str,
        endpoint_1: JujuIntegrationApplication,
        endpoint_2: JujuIntegrationApplication,
        timeout: timedelta,
    ):
        raise NotImplementedError

    @abstractmethod
    def wait_for_removal_of_units(self, model: str, applications: list[str], timeout: timedelta):
        raise NotImplementedError
