# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared validation and observation for moderate CPU and memory stress."""

from datetime import timedelta
from functools import partial
from time import monotonic, sleep
from typing import Callable

import pytest
from chaos_client import MetaChaosClient
from juju import JujuClient, JujuModelHandle

ValidationKey = tuple[JujuModelHandle, str, str, str, int | None]


def run_moderate_stress(
    juju_client: JujuClient,
    chaos_tool_for_model: Callable[[JujuModelHandle], MetaChaosClient],
    target: tuple[JujuModelHandle, str, str],
    neighbor: tuple[JujuModelHandle | None, str, str],
    *,
    operation: str,
    inject: Callable[[MetaChaosClient, JujuModelHandle, str], timedelta],
    check_interval: timedelta,
    duration_margin: timedelta,
    recovery_timeout: timedelta,
) -> None:
    """Verify baseline, sustained moderate stress, cleanup and recovery for a pair."""
    if any(value <= timedelta(0) for value in (check_interval, duration_margin, recovery_timeout)):
        raise ValueError("Check interval, duration margin and recovery timeout must be positive.")
    model, application, _ = target
    chaos = chaos_tool_for_model(model)
    if not chaos.supports(operation):
        pytest.skip(f"No available chaos client supports '{operation}' for moderate stress.")
    endpoints = [target, (neighbor[0] or model, neighbor[1], neighbor[2])]
    models = list(dict.fromkeys(selected_model for selected_model, _, _ in endpoints))
    juju_client.multi_model_idle_for_period(models=models, timeout=recovery_timeout, strict_timeout=True)
    check_service_health(juju_client, endpoints)
    required = validate_baseline(juju_client, endpoints)
    units = juju_client.backend.application_units(model, application)
    if not units:
        pytest.fail("No target units found for moderate stress.")
    unit = sorted(units)[0]
    observation_error: BaseException | None = None
    try:
        started = monotonic()
        duration = inject(chaos, model, unit)
        if duration <= timedelta(0):
            raise ValueError("Resolved moderate stress duration must be positive.")
        juju_client.logger.info("Observing moderate %s on %s/%s for %s.", operation, model.uri, unit, duration)
        observe_moderate_pressure(
            chaos,
            model,
            unit,
            duration.total_seconds(),
            check_interval.total_seconds(),
            partial(check_service_health, juju_client, endpoints),
            partial(validate_required_coverage, juju_client, endpoints, required),
            stress_deadline=started + (duration + duration_margin).total_seconds(),
        )
    except BaseException as error:
        observation_error = error
        raise
    finally:
        try:
            chaos.cleanup_all()
        except BaseException as cleanup_error:
            if observation_error is not None:
                # Preserve the observation traceback and chain the cleanup failure.
                raise observation_error from cleanup_error
            raise
    juju_client.multi_model_idle_for_period(models=models, timeout=recovery_timeout, strict_timeout=True)
    check_service_health(juju_client, endpoints)
    validate_required_coverage(juju_client, endpoints, required)


def validate_service(juju_client: JujuClient, endpoints: list[tuple[JujuModelHandle, str, str]]) -> set[ValidationKey]:
    """Run simple validators and identify passing checks for the tested endpoints."""
    passed: set[ValidationKey] = set()
    for model in dict.fromkeys(model for model, _, _ in endpoints):
        applications = list(
            dict.fromkeys(application for selected_model, application, _ in endpoints if selected_model == model)
        )
        selected_endpoints = {
            (application, endpoint) for selected_model, application, endpoint in endpoints if selected_model == model
        }
        results = juju_client.validate_model(
            model=model, level="simple", applications=applications, endpoints=selected_endpoints
        )
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


def validate_baseline(juju_client: JujuClient, endpoints: list[tuple[JujuModelHandle, str, str]]) -> set[ValidationKey]:
    """Require a passing simple baseline for every selected endpoint before injection."""
    required = validate_service(juju_client, endpoints)
    covered_endpoints = {(model, unit.split("/")[0], endpoint) for model, unit, endpoint, _, _ in required}
    missing_endpoints = set(endpoints) - covered_endpoints
    if missing_endpoints:
        pytest.skip(
            f"No passing simple interface validators for selected endpoints: {missing_endpoints}; "
            "moderate stress is unverified."
        )
    return required


def check_service_health(juju_client: JujuClient, endpoints: list[tuple[JujuModelHandle, str, str]]) -> None:
    """Check each selected application once, including across models."""
    for model, application in dict.fromkeys((model, app) for model, app, _ in endpoints):
        juju_client.check_application_health(model=model, application=application)


def validate_required_coverage(
    juju_client: JujuClient,
    endpoints: list[tuple[JujuModelHandle, str, str]],
    required: set[ValidationKey],
) -> None:
    """Require every passing baseline check to remain available and passing."""
    missing = required - validate_service(juju_client, endpoints)
    if missing:
        raise RuntimeError(f"Required interface validation coverage disappeared: {missing}")


def observe_moderate_pressure(
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
            raise TimeoutError("Moderate stress exceeded its execution budget.")
        chaos.check_stress(model, unit)
        check_health()
        validate()
        check_health()
        chaos.check_stress(model, unit)
        if monotonic() >= stress_deadline:
            raise TimeoutError("Moderate stress exceeded its execution budget.")
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        sleep(min(interval, remaining))
