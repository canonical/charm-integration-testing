# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from functools import partial
from typing import Callable

import pytest
from chaos_client import MetaChaosClient
from juju import JujuClient, JujuModelHandle

from .moderate_stress import run_moderate_stress
from .scheduler.states import State


@pytest.fixture
def memory_pressure_workers() -> int:
    return 1


@pytest.fixture
def memory_pressure_duration() -> timedelta:
    return timedelta(minutes=5)


@pytest.fixture
def memory_pressure_size_mb() -> int:
    return 128


@pytest.fixture
def memory_pressure_duration_margin() -> timedelta:
    """Extra injection time for startup and the final validation round."""
    return timedelta(minutes=2)


@pytest.fixture
def memory_pressure_check_interval() -> timedelta:
    """Delay between validation rounds; validator runtime is additional."""
    return timedelta(seconds=10)


@pytest.fixture
def memory_pressure_recovery_timeout() -> timedelta:
    return timedelta(minutes=15)


def inject_memory_stress(
    chaos: MetaChaosClient,
    model: JujuModelHandle,
    unit: str,
    *,
    workers: int,
    size_mb: int,
    duration: timedelta,
    duration_margin: timedelta,
) -> timedelta:
    return chaos.stress_memory(
        model,
        unit,
        workers=workers,
        size_mb=size_mb,
        duration=duration,
        scenario="moderate_pressure",
        duration_margin=duration_margin,
    )


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
@pytest.mark.usefixtures("kubernetes_client")
def test_live_memory_stress_moderate(
    juju_client: JujuClient,
    chaos_tool_for_model: Callable[[JujuModelHandle], MetaChaosClient],
    target_model_ref: JujuModelHandle,
    target_application: str,
    target_endpoint: str,
    neighbor_model_ref: JujuModelHandle | None,
    neighbor_application: str,
    neighbor_endpoint: str,
    memory_pressure_check_interval: timedelta,
    memory_pressure_recovery_timeout: timedelta,
    memory_pressure_workers: int,
    memory_pressure_duration: timedelta,
    memory_pressure_size_mb: int,
    memory_pressure_duration_margin: timedelta,
) -> None:
    run_moderate_stress(
        juju_client,
        chaos_tool_for_model,
        (target_model_ref, target_application, target_endpoint),
        (neighbor_model_ref, neighbor_application, neighbor_endpoint),
        operation="stress_memory",
        inject=partial(
            inject_memory_stress,
            workers=memory_pressure_workers,
            size_mb=memory_pressure_size_mb,
            duration=memory_pressure_duration,
            duration_margin=memory_pressure_duration_margin,
        ),
        check_interval=memory_pressure_check_interval,
        duration_margin=memory_pressure_duration_margin,
        recovery_timeout=memory_pressure_recovery_timeout,
    )
