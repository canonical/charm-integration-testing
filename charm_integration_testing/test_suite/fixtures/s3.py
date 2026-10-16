# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
from collections.abc import Iterator, Mapping

import pytest
import requests
from juju import JujuModelHandle
from s3_storage import S3Provider
from s3_storage.environment import provider_from_environment

from test_suite.scheduler.states import STATES_WITHOUT_EXISTING_CONTROLLER, State


def cleanup_pending_s3_resources(providers: Mapping[str, S3Provider]) -> None:
    failures: list[Exception] = []
    for provider in providers.values():
        try:
            provider.cleanup_pending_resources()
        except Exception as exc:
            failures.append(exc)
    if failures:
        raise RuntimeError(f"Pending S3 cleanup failed for {len(failures)} provider(s)") from failures[0]


def can_cleanup_session_storage(current_state: State) -> bool:
    """Only clean buckets when this run owns the controller lifecycle."""
    return current_state in STATES_WITHOUT_EXISTING_CONTROLLER


@pytest.fixture(scope="session")
def cleanup_s3_buckets(request: pytest.FixtureRequest) -> bool:
    current_state = State(request.config.getoption("--current-state"))
    return can_cleanup_session_storage(current_state)


@pytest.fixture(scope="session")
def s3_providers(
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
) -> Iterator[dict[str, S3Provider]]:
    with requests.Session() as session:
        providers_by_resource: dict[str, S3Provider] = {}
        providers_by_model: dict[str, S3Provider] = {}
        models = [(target_model_ref, "TARGET_")]
        if neighbor_model_ref is not None:
            models.append((neighbor_model_ref, "NEIGHBOR_"))
        for model, prefix in models:
            provider = provider_from_environment(os.environ, session, prefix=prefix)
            if provider is None:
                provider = provider_from_environment(os.environ, session)
            if provider is None:
                continue
            provider = providers_by_resource.setdefault(provider.handle.resource_id, provider)
            providers_by_model[model.resource_id] = provider
        try:
            yield providers_by_model
        finally:
            cleanup_pending_s3_resources(providers_by_resource)
