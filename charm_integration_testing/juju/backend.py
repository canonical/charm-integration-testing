# Copyright 2024-2025 Canonical Ltd.
# See LICENSE file for licensing details.

import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import field
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from kubernetes_client import KubernetesClient
from pydantic.dataclasses import dataclass

from validators.base.validator import ValidationResult

from .handles import JujuModelHandle
from .models import JujuApplicationInfo, JujuConsumedOfferInfo, JujuIntegration, JujuIntegrationApplication
from .version import JujuVersion

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


def is_agent_disconnected(wait_state: JujuWaitState) -> bool:
    """True if any noncompliant unit agent is 'lost' (disconnected from the controller)."""
    return any(state is not None and state.status == "lost" for state in wait_state.noncompliant_unit_agents.values())


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
    def scale_application(self, model: JujuModelHandle, application: str, num: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def num_units(self, model: JujuModelHandle, application: str) -> int:
        raise NotImplementedError

    @abstractmethod
    def list_applications(self, model: JujuModelHandle) -> dict[str, JujuApplicationInfo]:
        raise NotImplementedError

    @abstractmethod
    def list_consumed_offers(self, model: JujuModelHandle) -> dict[str, JujuConsumedOfferInfo]:
        raise NotImplementedError

    @abstractmethod
    def list_offers(self, model: JujuModelHandle) -> set[str]:
        """Return the names of all offers defined in *model*."""
        raise NotImplementedError

    @abstractmethod
    def create_offer(self, model: JujuModelHandle, app: str, endpoints: list[str], offer_name: str) -> None:
        """Create an offer exposing *endpoints* of *app* in *model* under *offer_name*."""
        raise NotImplementedError

    @abstractmethod
    def list_integrations(self, model: JujuModelHandle) -> set[JujuIntegration]:
        raise NotImplementedError

    @abstractmethod
    def reboot_model_controller(self, model: JujuModelHandle) -> None:
        raise NotImplementedError

    @abstractmethod
    def is_k8s_model(self, model: JujuModelHandle) -> bool:
        raise NotImplementedError

    def is_k8s_controller(self, controller: str) -> bool:
        """Return True if the controller is Kubernetes-based."""
        return self.is_k8s_model(JujuModelHandle(controller=controller, model="controller"))

    @abstractmethod
    def integration_exists(
        self, application_1: str, endpoint_1: str, application_2: str, endpoint_2: str, model: JujuModelHandle
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    def wait_idle(
        self,
        model: JujuModelHandle,
        timeout: timedelta | None,
        count: int | None,
        strict_timeout: bool = False,
        applications: list[str] | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_idle_multi_model(
        self,
        models: list[JujuModelHandle],
        timeout: timedelta | None,
        count: int | None,
        strict_timeout: bool = False,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_unhealthy(
        self,
        model: JujuModelHandle,
        application: str,
        timeout: timedelta | None,
        count: int | None,
        strict_timeout: bool = False,
    ) -> None:
        """Wait for *application*'s unit workload status to leave 'active' for *count* consecutive checks.

        Raises immediately if any unit agent disconnects.
        """
        raise NotImplementedError

    @abstractmethod
    def wait_application_settled(self, model: JujuModelHandle, application: str, timeout: timedelta | None) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_application_scaled(self, model: JujuModelHandle, application: str, timeout: timedelta | None) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_for_unit_message(self, model: JujuModelHandle, unit: str, message: str, timeout: timedelta | None) -> None:
        raise NotImplementedError

    @abstractmethod
    def juju_status_text(self, model: JujuModelHandle) -> str:
        raise NotImplementedError

    @abstractmethod
    def integrate(
        self, model: JujuModelHandle, target_1: JujuIntegrationApplication, target_2: JujuIntegrationApplication
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def remove_integration(
        self, model: JujuModelHandle, target_1: JujuIntegrationApplication, target_2: JujuIntegrationApplication
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def deploy_bundle_file(
        self,
        model: JujuModelHandle,
        bundle: str,
        timeout: timedelta | None = None,
        trust: bool = False,
        force: bool = False,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def refresh_application(
        self,
        model: JujuModelHandle,
        application: str,
        revision: int | None = None,
        channel: str | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def remove_applications(self, model: JujuModelHandle, *applications: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_for_removal(self, model: JujuModelHandle, applications: list[str], timeout: timedelta | None) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_for_removal_of_integration(
        self,
        model: JujuModelHandle,
        endpoint_1: JujuIntegrationApplication,
        endpoint_2: JujuIntegrationApplication,
        timeout: timedelta | None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_for_removal_of_units(
        self, model: JujuModelHandle, applications: list[str], timeout: timedelta | None
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_for_model_to_exist(self, model: JujuModelHandle, timeout: timedelta | None) -> None:
        raise NotImplementedError

    @abstractmethod
    def application_charm(self, model: JujuModelHandle, application: str) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def application_units(self, model: JujuModelHandle, application: str) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def exec_unit(self, model: JujuModelHandle, unit: str, task: str, operator: bool = False) -> JujuExecOutput:
        raise NotImplementedError

    @abstractmethod
    def run_action(self, model: JujuModelHandle, unit: str, action: str, arguments: dict[str, Any]) -> JujuTask:
        raise NotImplementedError

    @abstractmethod
    def add_secret(self, model: JujuModelHandle, name: str, values: dict[str, str]) -> str:
        raise NotImplementedError

    @abstractmethod
    def read_secret(self, model: JujuModelHandle, name_or_id: str) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    def grant_secret(self, model: JujuModelHandle, name_or_id: str, application: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def remove_secret(self, model: JujuModelHandle, name_or_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def deploy_application(
        self,
        model: JujuModelHandle,
        charm: str,
        application: str | None = None,
        config: dict[str, Any] | None = None,
        trust: bool = False,
        force: bool = False,
        channel: str | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def configure_application(self, model: JujuModelHandle, application: str, values: dict[str, str]) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_application_config(self, model: JujuModelHandle, application: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def bootstrap_controller(
        self,
        cloud: str,
        controller: str,
        controller_constraints: dict[str, str],
        bootstrap_configuration: dict[str, str],
        metadata_source: Path | None = None,
        agent_version: str | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def add_model(self, controller: str, model: str, model_config: dict[str, str]) -> None:
        raise NotImplementedError

    @abstractmethod
    def scp(self, model: JujuModelHandle, source: str, destination: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def ssh(self, model: JujuModelHandle, application: str, command: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def unit_ip(self, model: JujuModelHandle, unit: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def version(self, model: JujuModelHandle) -> JujuVersion:
        raise NotImplementedError

    @abstractmethod
    def cli_version(self) -> JujuVersion:
        raise NotImplementedError

    @abstractmethod
    def validate_application(
        self, model: JujuModelHandle, application: str, level: str
    ) -> dict[str, list[ValidationResult]]:
        raise NotImplementedError

    @abstractmethod
    def kill_controller(self, controller: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def migrate_model(self, model_name: str, source_controller: str, target_controller: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def upgrade_controller(self, controller: str, agent_version: str | None = None) -> None:
        raise NotImplementedError

    @abstractmethod
    def upgrade_model(self, model: JujuModelHandle, agent_version: str | None = None) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_for_application_revision(
        self,
        application: str,
        expected_revision: int,
        timeout: timedelta | None,
        model: JujuModelHandle,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def debug_log(self, model: JujuModelHandle) -> str:
        raise NotImplementedError

    @abstractmethod
    def get_controller_kubeconfig(self, controller: str) -> Path | None:
        """Return the kubeconfig path for a K8s controller's cloud, or None for machine controllers."""
        raise NotImplementedError

    @abstractmethod
    def get_kubernetes_client_for_controller(self, controller: str) -> KubernetesClient | None:
        """Return a KubernetesClient for a K8s controller's cloud, or None for machine controllers.

        Resolution (cloud type, kubeconfig lookup, client construction/caching) is
        delegated entirely to the backend, which is the single source of truth for
        controller cloud configuration.
        """
        raise NotImplementedError
