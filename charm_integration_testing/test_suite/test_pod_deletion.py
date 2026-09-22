# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
from datetime import timedelta

import pytest
from juju import JujuClient, JujuModelHandle, PersistenceKey
from kubernetes_client import KubernetesClient, PodStatus

from validators.base import PersistenceState

from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
def test_pod_deletion(
    juju_client: JujuClient,
    _is_running_on_kubernetes: None,
    kubernetes_client: KubernetesClient | None,
    target_model_ref: JujuModelHandle,
    neighbor_model_ref: JujuModelHandle | None,
    target_application: str,
    persistence_state: dict[PersistenceKey, PersistenceState],
) -> None:
    if kubernetes_client is None:
        pytest.fail("KubernetesClient was not instantiated correctly. Is KUBECONFIG set?")

    namespace = target_model_ref.model
    pods = kubernetes_client.get_charm_pods(application_name=target_application, model=namespace)
    assert len(pods) > 0, f"No pods found in namespace {namespace} to delete."

    existing_uids = {pod.metadata.uid for pod in pods}
    pod_to_delete = pods[0]
    kubernetes_client.delete_pod(namespace=namespace, pod_name=pod_to_delete.metadata.name)

    # Wait for a new pod to be created. Passing every pre-existing UID (not just the deleted
    # pod's) ensures an untouched sibling replica can't be mistaken for the deleted pod's
    # replacement when the application has multiple replicas.
    new_pod = kubernetes_client.wait_for_new_pod(
        namespace=namespace,
        application_name=target_application,
        existing_uids=existing_uids,
        timeout=timedelta(minutes=15),
    )

    # Then wait for that specific new pod to become ready.
    kubernetes_client.wait_for_pod_status(
        pod_name=new_pod.metadata.name,
        namespace=namespace,
        target_status=PodStatus.RUNNING,
        timeout=timedelta(minutes=15),
    )

    # Wait for return to idle. Also wait on the neighbor model (pod deletion/recreation can
    # trigger relation hooks there).
    models_to_settle = [target_model_ref] + ([neighbor_model_ref] if neighbor_model_ref is not None else [])
    juju_client.multi_model_idle_for_period(models_to_settle, timeout=timedelta(minutes=15))

    # Validate all applications and relations. For a CMR the persistence validator lives on the
    # neighbor's requirer units, so checkpoint there too.
    for model_ref in (m for m in (target_model_ref, neighbor_model_ref) if m is not None):
        juju_client.validate_model(
            model=model_ref, level="simple", persistence="checkpoint", persistence_state=persistence_state
        )
