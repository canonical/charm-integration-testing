# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from functools import partial
from typing import Callable

import pytest
from chaos_client import MetaChaosClient
from juju import JujuClient, JujuModelHandle
from kubernetes_client import KubernetesClientRegistry

from .kubernetes import require_model_kubeconfig
from .moderate_stress import run_moderate_stress
from .scheduler.states import State


@pytest.fixture
def cpu_pressure_workers() -> int:
    return 1


@pytest.fixture
def cpu_pressure_duration() -> timedelta:
    return timedelta(minutes=5)


@pytest.fixture
def cpu_pressure_check_interval() -> timedelta:
    """Delay between rounds; validator runtime is additional."""
    return timedelta(seconds=10)


@pytest.fixture
def cpu_pressure_duration_margin() -> timedelta:
    """Extra injection time for startup and the final validation round, not observation time."""
    return timedelta(minutes=2)


@pytest.fixture
def cpu_pressure_recovery_timeout() -> timedelta:
    return timedelta(minutes=15)


def inject_cpu_stress(
    chaos: MetaChaosClient,
    model: JujuModelHandle,
    unit: str,
    *,
    workers: int,
    duration: timedelta,
    duration_margin: timedelta,
) -> timedelta:
    return chaos.stress_cpu(
        model,
        unit,
        workers=workers,
        duration=duration,
        scenario="moderate_pressure",
        duration_margin=duration_margin,
    )


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
def test_live_cpu_stress_moderate(
    juju_client: JujuClient,
    chaos_tool_for_model: Callable[[JujuModelHandle], MetaChaosClient],
    target_model_ref: JujuModelHandle,
    target_application: str,
    target_endpoint: str,
    neighbor_model_ref: JujuModelHandle | None,
    neighbor_application: str,
    neighbor_endpoint: str,
    cpu_pressure_check_interval: timedelta,
    cpu_pressure_duration_margin: timedelta,
    cpu_pressure_recovery_timeout: timedelta,
    cpu_pressure_workers: int,
    cpu_pressure_duration: timedelta,
    kubernetes_client_registry: KubernetesClientRegistry,
) -> None:
    require_model_kubeconfig(juju_client.backend, kubernetes_client_registry, target_model_ref)
    run_moderate_stress(
        juju_client,
        chaos_tool_for_model,
        (target_model_ref, target_application, target_endpoint),
        (neighbor_model_ref, neighbor_application, neighbor_endpoint),
        operation="stress_cpu",
        inject=partial(
            inject_cpu_stress,
            workers=cpu_pressure_workers,
            duration=cpu_pressure_duration,
            duration_margin=cpu_pressure_duration_margin,
        ),
        check_interval=cpu_pressure_check_interval,
        duration_margin=cpu_pressure_duration_margin,
        recovery_timeout=cpu_pressure_recovery_timeout,
    )
