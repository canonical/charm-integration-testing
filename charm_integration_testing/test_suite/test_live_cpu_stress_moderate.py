# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from functools import partial
from time import monotonic
from typing import Callable

import pytest
from chaos_client import MetaChaosClient
from juju import JujuClient, JujuModelHandle

from .moderate_stress import (
    check_service_health,
    observe_moderate_pressure,
    validate_required_coverage,
    validate_service,
)
from .scheduler.states import State

CPU_PRESSURE_WORKERS = 1
CPU_PRESSURE_DURATION = timedelta(minutes=5)


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
) -> None:
    if any(
        value <= timedelta(0)
        for value in (cpu_pressure_check_interval, cpu_pressure_duration_margin, cpu_pressure_recovery_timeout)
    ):
        raise ValueError("CPU pressure check interval, duration margin and recovery timeout must be positive.")
    chaos = chaos_tool_for_model(target_model_ref)
    if not chaos.supports("stress_cpu"):
        pytest.skip("Moderate CPU stress requires Litmus or Chaos Mesh with CPU stress support.")
    endpoints = [
        (target_model_ref, target_application, target_endpoint),
        (neighbor_model_ref or target_model_ref, neighbor_application, neighbor_endpoint),
    ]
    models = list(dict.fromkeys(model for model, _, _ in endpoints))
    juju_client.multi_model_idle_for_period(models=models, timeout=cpu_pressure_recovery_timeout, strict_timeout=True)

    check_service_health(juju_client, endpoints)
    required = validate_service(juju_client, endpoints)
    covered_endpoints = {(model, unit.split("/")[0], endpoint) for model, unit, endpoint, _, _ in required}
    missing_endpoints = set(endpoints) - covered_endpoints
    if missing_endpoints:
        pytest.skip(
            f"No passing simple interface validators for selected endpoints: {missing_endpoints}; "
            "moderate stress is unverified."
        )

    units = juju_client.backend.application_units(target_model_ref, target_application)
    if not units:
        pytest.fail("No target units found for moderate CPU stress.")
    unit = sorted(units)[0]
    observation_error: BaseException | None = None
    try:
        started = monotonic()
        duration = chaos.stress_cpu(
            target_model_ref,
            unit,
            workers=CPU_PRESSURE_WORKERS,
            duration=CPU_PRESSURE_DURATION,
            scenario="moderate_pressure",
            duration_margin=cpu_pressure_duration_margin,
        )
        if duration <= timedelta(0):
            raise ValueError("Resolved CPU pressure duration must be positive.")
        juju_client.logger.info("Observing moderate CPU stress on %s/%s for %s.", target_model_ref.uri, unit, duration)
        observe_moderate_pressure(
            chaos,
            target_model_ref,
            unit,
            duration.total_seconds(),
            cpu_pressure_check_interval.total_seconds(),
            partial(check_service_health, juju_client, endpoints),
            partial(validate_required_coverage, juju_client, endpoints, required),
            stress_deadline=started + (duration + cpu_pressure_duration_margin).total_seconds(),
        )
    except BaseException as error:
        observation_error = error
        raise
    finally:
        try:
            chaos.cleanup_all()
        except BaseException as cleanup_error:
            if observation_error is not None:
                raise cleanup_error from observation_error
            raise

    juju_client.multi_model_idle_for_period(models=models, timeout=cpu_pressure_recovery_timeout, strict_timeout=True)
    check_service_health(juju_client, endpoints)
    validate_required_coverage(juju_client, endpoints, required)
