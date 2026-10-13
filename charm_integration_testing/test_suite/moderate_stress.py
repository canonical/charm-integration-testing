# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared validation and observation for moderate CPU and memory stress."""

from time import monotonic, sleep
from typing import Callable

from chaos_client import MetaChaosClient
from juju import JujuClient, JujuModelHandle

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
