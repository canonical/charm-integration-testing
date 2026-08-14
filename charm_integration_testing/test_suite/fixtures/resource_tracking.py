# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures that drive per-state Kubernetes resource tracking.

Registered as a pytest plugin via ``pytest_plugins`` in ``conftest.py`` so the
resource-tracking machinery lives in one place.  After every passing,
state-marked test the autouse fixture snapshots the resources present in each
model; the recorded observations are diffed into discrepancies at the end of the
suite.  Which resource *kinds* a charm version opts out of is resolved through
the bundle-builder ``OverridesClient`` using the charm and channel read from the
live model, so no charm names are hard-coded here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

import pytest
from juju import JujuClient
from juju.resource_registry import JujuModelHandle
from kubernetes_client import KubernetesClient
from resource_registry import ResourceRegistry
from resource_tracking import (
    KubernetesResourceCollector,
    ResourceCollector,
    StateResourceTracker,
)

from bundle_builder_x import CharmChannel, OverridesClient
from test_suite.conftest import failure_message, skipped_message
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


@pytest.fixture
def resource_tracking_skips_by_application(
    request: pytest.FixtureRequest,
    juju_client: JujuClient,
    session_resource_registry: ResourceRegistry,
    logger: logging.Logger,
) -> dict[str, frozenset[str]]:
    """Map each deployed application to the resource kinds it opts out of tracking.

    Overrides are declared per *charm version* under ``overrides`` in
    ``static/charm-overrides/<charm>.yaml``, but resources are attributed to an
    *application* on the cluster.  The application-to-charm mapping is read from
    the live models via ``juju_client.list_applications`` rather than from
    hard-coded target/neighbor options, so charms pulled in as dependencies are
    resolved the same way.  Resolution is best-effort: a model that cannot be
    queried simply contributes no skips.
    """
    overrides_client = _resource_tracking_overrides_client(request, logger)
    if overrides_client is None:
        return {}

    skips: dict[str, frozenset[str]] = {}
    for handle in session_resource_registry.registered_handles():
        if not isinstance(handle, JujuModelHandle):
            continue
        try:
            applications = juju_client.list_applications(model=handle)
        except Exception:
            logger.debug("Could not list applications for model '%s'.", handle.model, exc_info=True)
            continue
        for application, info in applications.items():
            if info.channel is None:
                continue
            channel = CharmChannel.model_validate(str(info.channel))
            resolved = overrides_client.get_charm_resource_tracking_skips(info.charm, channel)
            if resolved:
                skips[application] = resolved
    return skips


@pytest.fixture(autouse=True)
def track_state_resources(
    request: pytest.FixtureRequest,
    kubernetes_client: KubernetesClient | None,
    session_resource_registry: ResourceRegistry,
    state_resource_tracker: StateResourceTracker,
    resource_tracking_skips_by_application: dict[str, frozenset[str]],
    logger: logging.Logger,
) -> Iterator[None]:
    """Snapshot resources per scheduler state after each passing state-marked test.

    Collection is deliberately best-effort and never fails the test: the
    resulting discrepancies are reported separately at the end of the suite.
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

    # Build a collector per available substrate.  Only Kubernetes is supported
    # today, but adding an lxd/openstack collector here keeps the tracker itself
    # substrate-agnostic.
    collectors: list[ResourceCollector] = []
    if kubernetes_client is not None:
        collectors.append(
            KubernetesResourceCollector(
                kubernetes_client,
                session_resource_registry,
                resource_skips=resource_tracking_skips_by_application,
            )
        )
    if not collectors:
        return

    state_resource_tracker.collect(marker.provides, collectors, logger)
