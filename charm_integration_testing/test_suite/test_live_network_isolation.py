# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta
from pathlib import Path

import pytest
from chaos_client import MetaChaosClient
from juju import JujuClient, JujuModelHandle
from kubernetes_client import KubernetesClient

from .scheduler.states import State
from .temporary_network_probe import NetworkIsolationProbe


@pytest.mark.state(requires=State.DEPLOYED)
def test_live_network_isolation(
    juju_client: JujuClient,
    require_chaos_tool: MetaChaosClient,
    kubernetes_client: KubernetesClient | None,
    target_model_ref: JujuModelHandle,
    target_application: str,
    cloud_kubeconfigs: dict[str, Path],
    target_cloud: str,
) -> None:
    if kubernetes_client is None:
        pytest.skip("Network isolation requires Kubernetes.")

    unit = f"{target_application}/0"

    # Establish a healthy baseline so an existing failure cannot satisfy the test.
    juju_client.idle_for_period(model=target_model_ref, timeout=timedelta(minutes=15), strict_timeout=True)
    # TEMPORARY SQT-909: remove this diagnostic lifecycle after investigation.
    probe = NetworkIsolationProbe(
        kubernetes_client.backend, cloud_kubeconfigs.get(target_cloud), target_model_ref.model, unit
    )
    try:
        probe.prepare()
        probe.sample("before isolation")
        try:
            require_chaos_tool.isolate_network(model=target_model_ref.model, unit=unit)
            probe.sample("after policy creation")
            # Debounced against update-status blips; agent disconnection and timeout fail the test.
            juju_client.unhealthy_for_period(
                target_application, model=target_model_ref, timeout=timedelta(minutes=10), strict_timeout=True
            )
        finally:
            try:
                probe.sample("before policy removal")
            finally:
                removed = False
                try:
                    require_chaos_tool.remove_network_isolation(model=target_model_ref.model, unit=unit)
                    removed = True
                finally:
                    probe.after_removal(removed)
    finally:
        probe.cleanup()

    # Wait for self-recovery
    juju_client.idle_for_period(model=target_model_ref, timeout=timedelta(minutes=15), strict_timeout=True)
    juju_client.validate_model(model=target_model_ref, level="simple")
