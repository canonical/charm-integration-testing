# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
from datetime import timedelta

import pytest
from juju import JujuClient
from kubernetes_client import KubernetesClient, PodStatus

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
def test_pod_deletion(
    juju_client: JujuClient,
    kubernetes_client: KubernetesClient,
    model: str,
    target_application: str,
) -> None:
    pods = kubernetes_client.get_charm_pods(charm=target_application, model=model)
    assert len(pods) > 0, f"No pods found in namespace {model} to delete."

    pod_to_delete = pods[0]
    kubernetes_client.delete_pod(namespace=model, pod_name=pod_to_delete.metadata.name)

    # Wait for the pod to be deleted and a new one to be created
    kubernetes_client.wait_for_pod_recreation(
        namespace=model,
        pod_name=pod_to_delete.metadata.name,
        target_status=PodStatus.RUNNING,
        timeout=timedelta(minutes=1),
        delay=timedelta(seconds=1),
    )

    # Wait for return to idle
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=5))

    # Validate all applications and relations
    juju_client.validate_model(model=model, level="simple")
