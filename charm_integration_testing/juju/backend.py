# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

import warnings
from abc import ABC, abstractmethod
from dataclasses import field
from datetime import datetime, timedelta
from functools import wraps

from pydantic.dataclasses import dataclass


class JujuPerformanceWarning(UserWarning):
    """Base warning for Juju performance issues."""


class JujuStatusPerformanceWarning(JujuPerformanceWarning):
    """Warning when juju status operations are slow."""


def warn_performance(threshold: timedelta | None = None, category: type[Warning] = JujuPerformanceWarning):
    """Decorator that emits a warning if a function takes longer than threshold.

    Args:
        threshold: Time threshold as timedelta. Defaults to 5 seconds if None.
        category: Warning class to emit
    """
    # Determine the threshold to use
    if threshold is None:
        threshold = timedelta(seconds=5)

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_time = datetime.now()
            result = None
            try:
                result = func(*args, **kwargs)
            finally:
                if (datetime.now() - start_time) > threshold:
                    warnings.warn(f"Exceeded threshold of {threshold.total_seconds():.1f}s", category, stacklevel=2)
            return result

        return wrapper

    return decorator


@dataclass(frozen=True)
class JujuApplicationState:
    charm: str
    revision: int
    status: str
    message: str


@dataclass(frozen=True)
class JujuUnitState(JujuApplicationState):
    pass


@dataclass(frozen=True)
class JujuWaitState:
    message: str = "waiting"
    insufficient_status_checks: bool = False
    noncompliant_applications: dict[str, JujuApplicationState | None] = field(default_factory=dict)
    noncompliant_units: dict[str, JujuUnitState | None] = field(default_factory=dict)


class JujuWaitTimeoutError(TimeoutError):
    wait_state: JujuWaitState

    def __init__(
        self,
        wait_state: JujuWaitState | None = None,
    ):
        self.wait_state = wait_state if wait_state is not None else JujuWaitState()

    def __str__(self) -> str:
        message = f"Timed out while {self.wait_state.message}"
        addendums = []
        if len(self.wait_state.noncompliant_applications) > 0:
            applications = [f"'{v}'" for v in sorted(self.wait_state.noncompliant_applications)]
            addendums.append(f"applications: [{', '.join(applications)}]")
        if len(self.wait_state.noncompliant_units) > 0:
            units = [f"'{v}'" for v in sorted(self.wait_state.noncompliant_units)]
            addendums.append(f"units: [{', '.join(units)}]")
        if self.wait_state.insufficient_status_checks:
            addendums.append("insufficient status checks")
        if len(addendums) > 0:
            message = f"{message} ({', '.join(sorted(addendums))})"
        return message

    def __repr__(self) -> str:
        return str(self)


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


@dataclass
class JujuExecOutput:
    return_code: int
    stdout: str
    stderr: str


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
    def integration_exists(
        self, application_1: str, endpoint_1: str, application_2: str, endpoint_2: str, model: str
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    def wait_idle(self, model: str, timeout: timedelta | None, count: int | None):
        raise NotImplementedError

    @abstractmethod
    def wait_application_settled(self, model: str, application: str, timeout: timedelta | None):
        raise NotImplementedError

    @abstractmethod
    def wait_application_scaled(self, model: str, application: str, timeout: timedelta | None):
        raise NotImplementedError

    @abstractmethod
    def wait_for_unit_message(self, model: str, unit: str, message: str, timeout: timedelta | None):
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
    def wait_for_removal(self, model: str, applications: list[str], timeout: timedelta | None):
        raise NotImplementedError

    @abstractmethod
    def wait_for_removal_of_integration(
        self,
        model: str,
        endpoint_1: JujuIntegrationApplication,
        endpoint_2: JujuIntegrationApplication,
        timeout: timedelta | None,
    ):
        raise NotImplementedError

    @abstractmethod
    def wait_for_removal_of_units(self, model: str, applications: list[str], timeout: timedelta | None):
        raise NotImplementedError

    @abstractmethod
    def application_charm(self, model: str, application: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def application_units(self, model: str, application: str) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def exec_unit(self, model: str, unit: str, task: str) -> JujuExecOutput:
        raise NotImplementedError

    @abstractmethod
    def run_action(self, model: str, unit: str, action: str, arguments: dict):
        raise NotImplementedError

    @abstractmethod
    def add_secret(self, model: str, name: str, values: dict[str, str]) -> str:
        raise NotImplementedError

    @abstractmethod
    def read_secret(self, model: str, name_or_id: str) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    def grant_secret(self, model: str, name_or_id: str, application: str):
        raise NotImplementedError

    @abstractmethod
    def remove_secret(self, model: str, name_or_id: str):
        raise NotImplementedError

    @abstractmethod
    def deploy_application(self, model: str, charm: str, application: str | None = None):
        raise NotImplementedError

    @abstractmethod
    def configure_application(self, model: str, application: str, values: dict[str, str]):
        raise NotImplementedError

    @abstractmethod
    def scp(self, model: str, source: str, destination: str):
        raise NotImplementedError

    @abstractmethod
    def ssh(self, model: str, application: str, command: str):
        raise NotImplementedError

    @abstractmethod
    def unit_ip(self, model: str, unit: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def get_charm_revisions(self, model: str) -> set[tuple[str, int]]:
        raise NotImplementedError
