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
    validate_baseline,
    validate_required_coverage,
)
from .scheduler.states import State


@pytest.fixture
def memory_pressure_check_interval() -> timedelta:
    """Delay between validation rounds; validator runtime is additional."""
    return timedelta(seconds=10)


@pytest.fixture
def memory_pressure_recovery_timeout() -> timedelta:
    return timedelta(minutes=15)


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
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
) -> None:
    interval = memory_pressure_check_interval.total_seconds()
    if interval <= 0 or memory_pressure_recovery_timeout.total_seconds() <= 0:
        raise ValueError("Check interval and recovery timeout must be positive.")
    chaos = chaos_tool_for_model(target_model_ref)
    if not chaos.supports("stress_memory"):
        pytest.skip("No available chaos client supports moderate memory stress.")
    endpoints = [
        (target_model_ref, target_application, target_endpoint),
        (neighbor_model_ref or target_model_ref, neighbor_application, neighbor_endpoint),
    ]
    models_to_validate = list(dict.fromkeys(model for model, _, _ in endpoints))
    juju_client.multi_model_idle_for_period(
        models=models_to_validate, timeout=memory_pressure_recovery_timeout, strict_timeout=True
    )

    check_service_health(juju_client, endpoints)
    required = validate_baseline(juju_client, endpoints)

    units = juju_client.backend.application_units(target_model_ref, target_application)
    if not units:
        pytest.fail("No target units found for moderate memory stress.")
    unit = sorted(units)[0]
    observation_error: BaseException | None = None
    margin = timedelta(minutes=2)
    injection_started = monotonic()
    try:
        duration = chaos.stress_memory(
            target_model_ref,
            unit,
            workers=1,
            size_mb=128,
            duration=timedelta(minutes=5),
            scenario="moderate_pressure",
            duration_margin=margin,
        )
        juju_client.logger.info(
            "Observing moderate memory stress on %s/%s for %s.", target_model_ref.uri, unit, duration
        )
        observe_moderate_pressure(
            chaos,
            target_model_ref,
            unit,
            duration.total_seconds(),
            interval,
            partial(check_service_health, juju_client, endpoints),
            partial(validate_required_coverage, juju_client, endpoints, required),
            stress_deadline=injection_started + (duration + margin).total_seconds(),
        )
    except BaseException as error:
        observation_error = error
        raise
    finally:
        try:
            chaos.cleanup_all()
        except Exception as cleanup_error:
            if observation_error is not None:
                # Keep the original failure and traceback, with cleanup retained in the exception chain.
                raise observation_error from cleanup_error
            raise
    juju_client.multi_model_idle_for_period(
        models=models_to_validate, timeout=memory_pressure_recovery_timeout, strict_timeout=True
    )
    check_service_health(juju_client, endpoints)
    validate_required_coverage(juju_client, endpoints, required)
