# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import os
from collections.abc import Callable, Iterator, Mapping
from functools import partial
from pathlib import Path

import pytest
import requests
from resource_registry import ResourceRegistry
from s3_storage import S3Provider, S3ProviderRegistry, S3ResourceRegistry
from s3_storage_swift import KeystoneResourceRegistry
from s3_storage_swift.configuration import provider_from_config

from test_suite.scheduler.states import STATES_WITHOUT_EXISTING_CONTROLLER, State


def s3_config_paths(environ: Mapping[str, str]) -> dict[str, Path]:
    """Read cloud aliases in the same form as KUBECONFIG_<cloud>."""
    configs: dict[str, Path] = {}
    for name, value in environ.items():
        value = value.strip()
        if not name.startswith("S3_CONFIG_") or not value:
            continue
        cloud = name.removeprefix("S3_CONFIG_").lower().replace("_", "-")
        if not cloud:
            raise ValueError("S3_CONFIG_<cloud> requires a cloud name")
        path = Path(value).expanduser().resolve()
        if cloud in configs and configs[cloud] != path:
            raise ValueError(f"Conflicting S3 configuration paths for cloud '{cloud}'")
        configs[cloud] = path
    return configs


def can_cleanup_session_storage(current_state: State) -> bool:
    """Only clean buckets when this run owns the controller lifecycle."""
    return current_state in STATES_WITHOUT_EXISTING_CONTROLLER


@pytest.fixture(scope="session")
def cleanup_s3_buckets(request: pytest.FixtureRequest) -> bool:
    current_state = State(request.config.getoption("--current-state"))
    return can_cleanup_session_storage(current_state)


@pytest.fixture(scope="session")
def s3_http_session() -> Iterator[requests.Session]:
    with requests.Session() as session:
        yield session


@pytest.fixture(scope="session")
def s3_provider_registry(
    s3_http_session: requests.Session,
    session_resource_registry: ResourceRegistry,
    cleanup_s3_buckets: bool,
) -> S3ProviderRegistry:
    resources = KeystoneResourceRegistry(session_resource_registry, cleanup_s3_buckets)
    return S3ProviderRegistry(
        s3_config_paths(os.environ),
        partial(provider_from_config, session=s3_http_session, resources=resources),
    )


@pytest.fixture(scope="session")
def s3_resources_for_provider(
    session_resource_registry: ResourceRegistry,
    cleanup_s3_buckets: bool,
) -> Callable[[S3Provider], S3ResourceRegistry]:
    """Share one lazily registered resource scope per provider across Juju clients."""
    scopes: dict[str, S3ResourceRegistry] = {}

    def resources_for_provider(provider: S3Provider) -> S3ResourceRegistry:
        resource_id = provider.handle.resource_id
        if resource_id not in scopes:
            scopes[resource_id] = S3ResourceRegistry(provider, session_resource_registry, cleanup_s3_buckets)
        return scopes[resource_id]

    return resources_for_provider
