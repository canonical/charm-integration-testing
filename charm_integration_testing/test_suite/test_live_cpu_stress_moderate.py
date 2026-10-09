# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from time import monotonic, sleep
from typing import Callable

import pytest
from chaos_client import MetaChaosClient
from juju import JujuClient, JujuModelHandle

from .scheduler.states import State

CPU_PRESSURE_WORKERS = 1
CPU_PRESSURE_DURATION = timedelta(minutes=5)
ValidationKey = tuple[JujuModelHandle, str, str, str, int | None]


def validate_service(juju_client: JujuClient, endpoints: list[tuple[JujuModelHandle, str, str]]) -> set[ValidationKey]:
    """Identify passing simple checks on the tested endpoints through the Juju facade."""
    passed: set[ValidationKey] = set()
    for model in dict.fromkeys(model for model, _, _ in endpoints):
        applications = list(dict.fromkeys(app for selected_model, app, _ in endpoints if selected_model == model))
        results = juju_client.validate_model(model=model, level="simple", applications=applications)
        selected = {(app, endpoint) for selected_model, app, endpoint in endpoints if selected_model == model}
        for unit, validations in results.items():
            for result in validations:
                if (
                    result.status == "PASS"
                    and result.level == "simple"
                    and (unit.split("/", 1)[0], result.endpoint) in selected
                ):
                    passed.add((model, unit, result.endpoint, result.interface, result.relation_id))
    return passed


def observe_cpu_pressure(
    chaos: MetaChaosClient,
    model: JujuModelHandle,
    unit: str,
    duration: timedelta,
    interval: timedelta,
    stress_deadline: float,
    check_health: Callable[[], None],
    validate: Callable[[], None],
) -> None:
    """Sample through the resolved duration, rejecting rounds that outlive the stress budget."""
    deadline = monotonic() + duration.total_seconds()
    while True:
        if monotonic() >= stress_deadline:
            raise TimeoutError("CPU pressure observation exceeded the duration plus safety margin.")
        chaos.check_stress(model, unit)
        check_health()
        validate()
        check_health()
        chaos.check_stress(model, unit)
        if monotonic() >= stress_deadline:
            raise TimeoutError("CPU pressure validation round exceeded the duration plus safety margin.")
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        sleep(min(interval.total_seconds(), remaining))


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

    def check_health() -> None:
        for model, application in dict.fromkeys((model, app) for model, app, _ in endpoints):
            juju_client.check_application_health(model=model, application=application)

    check_health()
    required = validate_service(juju_client, endpoints)
    if not required:
        pytest.skip("No passing simple validators for the tested endpoints; moderate CPU stress is unverified.")

    def validate() -> None:
        missing = required - validate_service(juju_client, endpoints)
        if missing:
            raise RuntimeError(f"Required interface validation coverage disappeared: {missing}")

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
        observe_cpu_pressure(
            chaos,
            target_model_ref,
            unit,
            duration,
            cpu_pressure_check_interval,
            started + (duration + cpu_pressure_duration_margin).total_seconds(),
            check_health,
            validate,
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
    check_health()
    validate()
