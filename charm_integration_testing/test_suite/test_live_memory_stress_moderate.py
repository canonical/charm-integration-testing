# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from time import monotonic, sleep
from typing import Callable

import pytest
from chaos_client import MetaChaosClient
from juju import JujuClient, JujuModelHandle
from kubernetes_client import KubernetesClient

from .fixtures.chaos_tools import ChaosTool, available_chaos_tools
from .scheduler.states import State

ValidationKey = tuple[JujuModelHandle, str, str, str, int | None]


def validate_service(juju_client: JujuClient, endpoints: list[tuple[JujuModelHandle, str, str]]) -> set[ValidationKey]:
    """Run simple validators and identify passing checks for the tested endpoints."""
    passed: set[ValidationKey] = set()
    for model in dict.fromkeys(model for model, _, _ in endpoints):
        applications = list(
            dict.fromkeys(application for selected_model, application, _ in endpoints if selected_model == model)
        )
        results = juju_client.validate_model(model=model, level="simple", applications=applications)
        for selected_model, application, endpoint in endpoints:
            if selected_model != model:
                continue
            for unit, validations in results.items():
                if unit.split("/")[0] != application:
                    continue
                for result in validations:
                    if result.status == "PASS" and result.level == "simple" and result.endpoint == endpoint:
                        passed.add((model, unit, result.endpoint, result.interface, result.relation_id))
    return passed


def observe_memory_pressure(
    chaos: MetaChaosClient,
    model: JujuModelHandle,
    unit: str,
    seconds: float,
    interval: float,
    check_health: Callable[[], None],
    validate: Callable[[], None],
    stress_deadline: float,
) -> None:
    """Sample health and run validators repeatedly while stress remains active."""
    deadline = monotonic() + seconds
    while True:
        if monotonic() >= stress_deadline:
            raise TimeoutError("Memory stress exceeded its execution budget.")
        chaos.check_stress(model, unit)
        check_health()
        validate()
        check_health()
        chaos.check_stress(model, unit)
        if monotonic() >= stress_deadline:
            raise TimeoutError("Memory stress exceeded its execution budget.")
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        sleep(min(interval, remaining))


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
    kubernetes_client: KubernetesClient | None,
    memory_pressure_check_interval: timedelta,
    memory_pressure_recovery_timeout: timedelta,
) -> None:
    if kubernetes_client is None:
        pytest.skip("Moderate memory stress requires Kubernetes.")
    tools = available_chaos_tools(kubernetes_client.backend)
    if not tools or (
        ChaosTool.LITMUS not in tools and not kubernetes_client.backend.crd_exists("stresschaos.chaos-mesh.org")
    ):
        pytest.skip("Moderate memory stress requires Litmus or Chaos Mesh with StressChaos.")
    interval = memory_pressure_check_interval.total_seconds()
    if interval <= 0 or memory_pressure_recovery_timeout.total_seconds() <= 0:
        raise ValueError("Check interval and recovery timeout must be positive.")
    endpoints = [
        (target_model_ref, target_application, target_endpoint),
        (neighbor_model_ref or target_model_ref, neighbor_application, neighbor_endpoint),
    ]
    models_to_validate = list(dict.fromkeys(model for model, _, _ in endpoints))
    juju_client.multi_model_idle_for_period(
        models=models_to_validate, timeout=memory_pressure_recovery_timeout, strict_timeout=True
    )

    def check_health() -> None:
        for model, application in dict.fromkeys((model, app) for model, app, _ in endpoints):
            juju_client.check_application_health(model=model, application=application)

    check_health()
    required = validate_service(juju_client, endpoints)
    if not required:
        pytest.skip("No passing simple interface validators for the tested service; moderate stress is unverified.")

    def validate() -> None:
        passed = validate_service(juju_client, endpoints)
        missing = required - passed
        if missing:
            raise RuntimeError(f"Required interface validation coverage disappeared: {missing}")

    units = juju_client.backend.application_units(target_model_ref, target_application)
    if not units:
        pytest.fail("No target units found for moderate memory stress.")
    unit = sorted(units)[0]
    chaos = chaos_tool_for_model(target_model_ref)
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
        observe_memory_pressure(
            chaos,
            target_model_ref,
            unit,
            duration.total_seconds(),
            interval,
            check_health,
            validate,
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
                raise RuntimeError(
                    f"Moderate memory stress failed: {observation_error!r}; cleanup also failed: {cleanup_error!r}"
                ) from cleanup_error
            raise
    juju_client.multi_model_idle_for_period(
        models=models_to_validate, timeout=memory_pressure_recovery_timeout, strict_timeout=True
    )
    check_health()
    validate()
