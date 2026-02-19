# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import field
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from pydantic.dataclasses import dataclass

from .models import JujuApplicationInfo, JujuIntegration, JujuIntegrationApplication

_P = ParamSpec("_P")
_R = TypeVar("_R")


class JujuPerformanceWarning(UserWarning):
    """Base warning for Juju performance issues."""


class JujuStatusPerformanceWarning(JujuPerformanceWarning):
    """Warning when juju status operations are slow."""


def warn_performance(
    threshold: timedelta, category: type[Warning] = JujuPerformanceWarning
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Decorator that emits a warning if a function takes longer than threshold.

    Args:
        threshold: Time threshold as timedelta.
        category: Warning class to emit
    """

    def decorator(func: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(func)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
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
class JujuUnitAgentState:
    charm: str
    revision: int
    status: str
    message: str


@dataclass(frozen=True)
class JujuWaitState:
    message: str = "waiting"
    insufficient_status_checks: bool = False
    noncompliant_applications: dict[str, JujuApplicationState | None] = field(default_factory=dict)
    noncompliant_units: dict[str, JujuUnitState | None] = field(default_factory=dict)
    noncompliant_unit_agents: dict[str, JujuUnitAgentState | None] = field(default_factory=dict)


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
        if len(self.wait_state.noncompliant_unit_agents) > 0:
            unit_agents = [f"'{v}'" for v in sorted(self.wait_state.noncompliant_unit_agents)]
            addendums.append(f"unit agents: [{', '.join(unit_agents)}]")
        if self.wait_state.insufficient_status_checks:
            addendums.append("insufficient status checks")
        if len(addendums) > 0:
            message = f"{message} ({', '.join(sorted(addendums))})"
        return message

    def __repr__(self) -> str:
        return str(self)


@dataclass
class JujuExecOutput:
    return_code: int
    stdout: str
    stderr: str


@dataclass
class JujuTask:
    """Represents a Juju task, as used by Juju Actions to represent action results."""

    # For now, keeping this somewhat minimal and opinionated.
    # Not doing a full wrapper of jubilant.Task.
    id: str
    return_code: int
    status: str
    message: str
    output: str  # from results.output
    # Omitting log, stdout and stderr for now.  During testing these were blank or empty.
    # We can always add them later.


class JujuBackend(ABC):
    @abstractmethod
    def scale_application(self, model: str, application: str, num: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def num_units(self, model: str, application: str) -> int:
        raise NotImplementedError

    @abstractmethod
    def list_applications(self, model: str) -> dict[str, JujuApplicationInfo]:
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
    def wait_idle(
        self,
        model: str,
        timeout: timedelta | None,
        count: int | None,
        strict_timeout: bool = False,
        applications: list[str] | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_application_settled(self, model: str, application: str, timeout: timedelta | None) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_application_scaled(self, model: str, application: str, timeout: timedelta | None) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_for_unit_message(self, model: str, unit: str, message: str, timeout: timedelta | None) -> None:
        raise NotImplementedError

    @abstractmethod
    def juju_status_text(self, model: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def integrate(self, model: str, target_1: JujuIntegrationApplication, target_2: JujuIntegrationApplication) -> None:
        raise NotImplementedError

    @abstractmethod
    def remove_integration(
        self, model: str, target_1: JujuIntegrationApplication, target_2: JujuIntegrationApplication
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def deploy_bundle_file(self, model: str, bundle: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def remove_applications(self, model: str, *applications: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_for_removal(self, model: str, applications: list[str], timeout: timedelta | None) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_for_removal_of_integration(
        self,
        model: str,
        endpoint_1: JujuIntegrationApplication,
        endpoint_2: JujuIntegrationApplication,
        timeout: timedelta | None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_for_removal_of_units(self, model: str, applications: list[str], timeout: timedelta | None) -> None:
        raise NotImplementedError

    @abstractmethod
    def application_charm(self, model: str, application: str) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def application_units(self, model: str, application: str) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def exec_unit(self, model: str, unit: str, task: str) -> JujuExecOutput:
        raise NotImplementedError

    @abstractmethod
    def run_action(self, model: str, unit: str, action: str, arguments: dict[str, Any]) -> JujuTask:
        raise NotImplementedError

    @abstractmethod
    def add_secret(self, model: str, name: str, values: dict[str, str]) -> str:
        raise NotImplementedError

    @abstractmethod
    def read_secret(self, model: str, name_or_id: str) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    def grant_secret(self, model: str, name_or_id: str, application: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def remove_secret(self, model: str, name_or_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def deploy_application(
        self,
        model: str,
        charm: str,
        application: str | None = None,
        config: dict[str, Any] | None = None,
        trust: bool = False,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def configure_application(self, model: str, application: str, values: dict[str, str]) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_application_config(self, model: str, application: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def scp(self, model: str, source: str, destination: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def ssh(self, model: str, application: str, command: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def unit_ip(self, model: str, unit: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def version(self, model: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def validate_application(self, model: str, application: str, level: str) -> None:
        # In Phase 2, this will trigger the Ops framework's built-in validation:
        # In Phase 1, this is a no-op. The ValidatorInjectorExtension handles
        # the actual validation via the post_validate hook.
        raise NotImplementedError
