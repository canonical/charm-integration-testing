# Copyright 2025-2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from juju import JujuBackend, JujuClient, JujuModelHandle
from kubernetes_client import KubernetesClientRegistry

from .kubernetes import require_kubeconfig
from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
def test_controller_restart(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
    juju_backend: JujuBackend,
    kubernetes_client_registry: KubernetesClientRegistry,
) -> None:
    controller_model = JujuModelHandle(controller=target_model_ref.controller, model="controller")
    cloud = juju_backend.kubernetes_cloud_for_model(controller_model)
    if cloud is not None:
        require_kubeconfig(kubernetes_client_registry, cloud)

    # Reboot our controllers with a rolling reboot
    juju_client.reboot_model_controller(model=target_model_ref)

    models_to_validate = [m for m in (target_model_ref, neighbor_model_ref) if m is not None]

    # Wait for return to idle
    juju_client.multi_model_idle_for_period(models_to_validate, timeout=timedelta(minutes=15))

    # Validate all applications and relations, and verify canary data survived the reboot. For a
    # CMR the persistence validator lives on the neighbor's requirer units, so validate there too.
    for model_ref in models_to_validate:
        juju_client.validate_model(model=model_ref, level="deep")
