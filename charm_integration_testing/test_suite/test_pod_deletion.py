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
    _is_running_on_kubernetes: None,
    kubernetes_client: KubernetesClient | None,
    model: str,
    target_application: str,
) -> None:
    if kubernetes_client is None:
        pytest.fail("KubernetesClient was not instantiated correctly. Is KUBECONFIG set?")

    pods = kubernetes_client.get_charm_pods(application_name=target_application, model=model)
    assert len(pods) > 0, f"No pods found in namespace {model} to delete."

    existing_uids = {pod.metadata.uid for pod in pods}
    pod_to_delete = pods[0]
    kubernetes_client.delete_pod(namespace=model, pod_name=pod_to_delete.metadata.name)

    # Wait for a new pod to be created. Passing every pre-existing UID (not just the deleted
    # pod's) ensures an untouched sibling replica can't be mistaken for the deleted pod's
    # replacement when the application has multiple replicas.
    new_pod = kubernetes_client.wait_for_new_pod(
        namespace=model,
        application_name=target_application,
        existing_uids=existing_uids,
        timeout=timedelta(minutes=15),
    )

    # Then wait for that specific new pod to become ready.
    kubernetes_client.wait_for_pod_status(
        pod_name=new_pod.metadata.name,
        namespace=model,
        target_status=PodStatus.RUNNING,
        timeout=timedelta(minutes=15),
    )

    # Wait for return to idle
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))

    # Validate all applications and relations
    juju_client.validate_model(model=model, level="simple")
