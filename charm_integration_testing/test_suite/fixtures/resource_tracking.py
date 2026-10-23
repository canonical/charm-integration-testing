# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures that drive per-state Kubernetes resource tracking.

Registered as a pytest plugin via ``pytest_plugins`` in ``conftest.py`` so the
resource-tracking machinery lives in one place.  After every passing,
state-marked test the autouse fixture snapshots the resources present in each
model; the recorded observations are diffed into discrepancies at the end of the
suite.  Which resource *kinds* a charm version opts out of is resolved through
the bundle-builder ``OverridesClient`` using the charm, channel and Ubuntu base
read from the live model, so no charm names are hard-coded here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

import pytest
from juju import JujuClient, JujuModelHandle
from kubernetes_client import KubernetesClientRegistry
from resource_registry import ResourceRegistry
from resource_tracking import (
    DEFAULT_KUBERNETES_SOURCES,
    KubernetesResourceCollector,
    ResourceCollector,
    StateResourceTracker,
)

from bundle_builder_x import CharmChannel, OverridesClient
from test_suite.conftest import failure_message, skipped_message
from test_suite.fixtures.juju import JujuClientRegistry, get_registered_model_handles
from test_suite.scheduler.markers import read_state_marker


@pytest.fixture(scope="session")
def state_resource_tracker() -> StateResourceTracker:
    """Session-scoped tracker of per-state resource baselines and discrepancies."""
    return StateResourceTracker()


def _resource_tracking_overrides_client(
    request: pytest.FixtureRequest, logger: logging.Logger
) -> OverridesClient | None:
    """Build an :class:`OverridesClient` for ``--charm-overrides``, or ``None``.

    Returns ``None`` when no overrides directory is configured or it does not
    exist, so callers can treat "no overrides" as "no skips".
    """
    overrides_raw = request.config.getoption("--charm-overrides")
    if not overrides_raw:
        return None
    assert isinstance(overrides_raw, str)
    overrides_dir = Path(overrides_raw)
    if not overrides_dir.is_absolute():
        overrides_dir = Path(request.config.rootpath) / overrides_dir
    overrides_dir = overrides_dir.resolve()
    if not overrides_dir.is_dir():
        return None
    return OverridesClient(overrides=overrides_dir, logger=logger)


def _resolve_resource_tracking_skips(
    overrides_client: OverridesClient | None,
    juju_client: JujuClient | None,
    model_handles: list[JujuModelHandle],
) -> dict[tuple[str, str, str], frozenset[str]]:
    skips_by_application: dict[tuple[str, str, str], frozenset[str]] = {}
    if overrides_client is not None and juju_client is not None:
        for handle in model_handles:
            applications = juju_client.list_applications(model=handle)
            for application, info in applications.items():
                if info.channel is None or info.base is None:
                    continue
                channel = CharmChannel.model_validate(str(info.channel))
                resolved = overrides_client.get_charm_resource_tracking_skips(info.charm, channel, info.base)
                if resolved:
                    scope = (handle.controller, handle.model, application)
                    skips_by_application[scope] = skips_by_application.get(scope, frozenset()) | resolved
    return skips_by_application


@pytest.fixture
def resource_tracking_skips_by_application(
    request: pytest.FixtureRequest,
    session_resource_registry: ResourceRegistry,
    juju_client_registry: JujuClientRegistry,
    logger: logging.Logger,
) -> dict[tuple[str, str, str], frozenset[str]]:
    """Map each ``(controller, model, application)`` to the kinds it opts out of tracking.

    Overrides are declared per *charm version* under ``overrides`` in
    ``static/charm-overrides/<charm>.yaml``, but resources are attributed to an
    *application in a model on a controller* on the cluster.  The
    application-to-charm mapping is read from the live models via
    ``juju_client.list_applications`` rather than from hard-coded target/neighbor
    options, so charms pulled in as dependencies are resolved the same way.

    The returned map is scoped to ``(controller, model, application)`` and
    unions the skips of every track/base combination observed at this point:
    because all of a model's states share one namespace, a resource kind
    retained by any track or base exercised there must be opted out for the
    whole model, while a different model keeps tracking the kind.
    """
    overrides_client = _resource_tracking_overrides_client(request, logger)
    return _resolve_resource_tracking_skips(
        overrides_client,
        juju_client_registry.client,
        get_registered_model_handles(session_resource_registry),
    )


@pytest.fixture(autouse=True)
def track_state_resources(
    request: pytest.FixtureRequest,
    kubernetes_client_registry: KubernetesClientRegistry,
    session_resource_registry: ResourceRegistry,
    juju_client_registry: JujuClientRegistry,
    state_resource_tracker: StateResourceTracker,
    resource_tracking_skips_by_application: dict[tuple[str, str, str], frozenset[str]],
    logger: logging.Logger,
) -> Iterator[None]:
    """Snapshot resources per scheduler state after each passing state-marked test.

    Collection is deliberately best-effort and never fails the test: the
    resulting discrepancies are reported separately at the end of the suite.
    Each model observation carries the skips resolved for its applications.
    Skips are resolved before each test and again after passing state-marked
    tests, so applications removed by a test and newly deployed applications
    are both represented.
    """
    yield

    # Only record when the environment state is known-good: the test must have a
    # state marker and must have passed (a failure or skip leaves the state
    # indeterminate, so the snapshot would be meaningless).
    if failure_message in request.node.stash or skipped_message in request.node.stash:
        return
    marker = read_state_marker(request.node)
    if marker is None:
        return
    client = juju_client_registry.client
    if client is None:
        return
    model_handles = get_registered_model_handles(session_resource_registry)
    if not model_handles:
        return
    post_test_skips = _resolve_resource_tracking_skips(
        _resource_tracking_overrides_client(request, logger),
        client,
        model_handles,
    )
    skips = dict(resource_tracking_skips_by_application)
    for scope, resource_types in post_test_skips.items():
        skips[scope] = skips.get(scope, frozenset()) | resource_types

    # Build a collector per supported substrate. Only Kubernetes is supported today; the collector will
    # skip models whose controllers are not Kubernetes-based.
    # Adding an lxd/openstack collector here keeps the tracker itself substrate-agnostic.
    collectors: list[ResourceCollector] = [
        KubernetesResourceCollector(
            client.backend,
            kubernetes_client_registry,
            session_resource_registry,
            sources=DEFAULT_KUBERNETES_SOURCES,
        )
    ]

    state_resource_tracker.collect(marker.provides, collectors, logger, skips=skips)
