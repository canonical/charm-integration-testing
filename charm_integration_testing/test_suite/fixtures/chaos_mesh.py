# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for detecting Chaos Mesh and gating chaos tests on its presence.

Registered as a pytest plugin via ``pytest_plugins`` in ``conftest.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from kubernetes_client import KubernetesBackend

STRESSCHAOS_CRD = "stresschaos.chaos-mesh.org"


def _chaos_mesh_installed(kubeconfig: Path) -> bool:
    """Return whether the StressChaos CRD is registered on kubeconfig's cluster."""
    backend = KubernetesBackend.k8s_client(kubeconfig=kubeconfig)
    try:
        return backend.crd_exists(STRESSCHAOS_CRD)
    finally:
        backend.api_client.close()


@pytest.fixture(scope="session", autouse=True)
def detect_chaos_mesh(
    request: pytest.FixtureRequest,
    cloud_kubeconfigs: dict[str, Path],
    logger: logging.Logger,
) -> None:
    """Log Chaos Mesh availability for every participating Kubernetes cloud, once per session."""
    target_platform = request.config.getoption("--target-platform")
    neighbor_platform = request.config.getoption("--neighbor-platform") or target_platform
    participants = (
        (request.config.getoption("--target-cloud"), target_platform),
        (request.config.getoption("--neighbor-cloud"), neighbor_platform),
    )
    checked_clouds: set[str] = set()

    for cloud, platform in participants:
        # Machine clouds have no Chaos Mesh story yet, so skip them.
        if not cloud or platform != "kubernetes" or cloud in checked_clouds:
            continue
        checked_clouds.add(cloud)

        kubeconfig = cloud_kubeconfigs.get(cloud)
        if kubeconfig is None:
            logger.warning(
                "Chaos Mesh detection unavailable for cloud %s: no kubeconfig configured.",
                cloud,
            )
            continue

        installed = _chaos_mesh_installed(kubeconfig)
        logger.info(
            "Chaos Mesh on cloud %s: %s",
            cloud,
            "detected" if installed else "not installed",
        )


@pytest.fixture
def require_chaos_mesh(
    target_platform: str,
    target_cloud: str,
    cloud_kubeconfigs: dict[str, Path],
) -> None:
    """Skip a Chaos Mesh-only test when the target cloud lacks it."""
    if target_platform != "kubernetes":
        pytest.skip("Chaos Mesh tests require a Kubernetes target.")

    kubeconfig = cloud_kubeconfigs.get(target_cloud)
    if kubeconfig is None:
        pytest.fail(f"Cannot check Chaos Mesh on cloud '{target_cloud}': no kubeconfig configured.")

    if not _chaos_mesh_installed(kubeconfig):
        pytest.skip(f"Chaos Mesh is not installed on cloud '{target_cloud}': CRD '{STRESSCHAOS_CRD}' is absent.")
