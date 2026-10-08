# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from datetime import timedelta

import pytest
from juju import JujuClient, JujuModelHandle
from kubernetes_client import KubernetesClient, PodStatus

from bundle_builder_x import Charm

from .ha_utils import require_principal_charm
from .scheduler.states import State


@pytest.mark.state(requires=State.DEPLOYED_HA, provides=State.DEPLOYED_HA)
def test_reboot_leader(
    juju_client: JujuClient,
    target_model_ref: JujuModelHandle,
    target_application: str,
    target_platform: str,
    target_deployed_charm: Charm | None,
    neighbor_model_ref: JujuModelHandle | None,
    neighbor_application: str | None,
    kubernetes_client: KubernetesClient | None,
) -> None:
    charm = require_principal_charm(target_deployed_charm)
    current_units = juju_client.num_units(target_application, model=target_model_ref)
    if current_units < charm.ha_units:
        pytest.fail(f"{target_application} has {current_units} units, fewer than its HA minimum of {charm.ha_units}.")

    leader = juju_client.application_leader(target_application, model=target_model_ref)
    old_pod_uids: set[str] = set()
    leader_pod_name: str | None = None
    leader_pod_uid: str | None = None
    if target_platform == "kubernetes":
        if kubernetes_client is None:
            pytest.fail("KubernetesClient was not instantiated correctly. Is KUBECONFIG set?")
        pods = kubernetes_client.get_charm_pods(target_application, model=target_model_ref.model)
        leader_pods = [
            pod
            for pod in pods
            if (pod.metadata.annotations or {}).get("unit.juju.is/id") == leader
            and pod.metadata.deletion_timestamp is None
        ]
        if len(leader_pods) != 1:
            pytest.fail(f"Expected one live Pod for leader {leader}, found {len(leader_pods)}.")
        if any(pod.metadata.uid is None for pod in pods):
            pytest.fail("Expected every target Pod to have a UID before restarting the leader.")
        old_pod_uids = {pod.metadata.uid for pod in pods if pod.metadata.uid is not None}
        leader_pod_name = leader_pods[0].metadata.name
        leader_pod_uid = leader_pods[0].metadata.uid
        if leader_pod_name is None:
            pytest.fail(f"Leader Pod for {leader} has no name.")
        if leader_pod_uid is None:
            pytest.fail(f"Leader Pod for {leader} has no UID.")

    models = [model for model in (target_model_ref, neighbor_model_ref) if model is not None]

    def restart_leader() -> None:
        if target_platform == "kubernetes":
            assert kubernetes_client is not None
            assert leader_pod_name is not None
            assert leader_pod_uid is not None
            kubernetes_client.delete_pod(target_model_ref.model, leader_pod_name)
            kubernetes_client.wait(
                check=lambda: (
                    True
                    if all(
                        pod.metadata.uid != leader_pod_uid
                        for pod in kubernetes_client.get_charm_pods(target_application, model=target_model_ref.model)
                    )
                    else None
                ),
                timeout_message=f"Leader Pod {leader_pod_name} was not removed.",
                timeout=timedelta(minutes=15),
            )
        else:
            juju_client.ssh(leader, "sudo reboot", model=target_model_ref)
            juju_client.wait_for_unit_unavailable(leader, model=target_model_ref, timeout=timedelta(minutes=15))

    def wait_for_recovery() -> None:
        if target_platform == "kubernetes":
            assert kubernetes_client is not None
            replacement = kubernetes_client.wait(
                check=lambda: next(
                    (
                        pod
                        for pod in kubernetes_client.get_charm_pods(
                            target_application, model=target_model_ref.model
                        )
                        if (pod.metadata.annotations or {}).get("unit.juju.is/id") == leader
                        and pod.metadata.uid is not None
                        and pod.metadata.uid not in old_pod_uids
                        and pod.metadata.deletion_timestamp is None
                    ),
                    None,
                ),
                timeout_message=f"Replacement Pod for leader {leader} did not appear.",
                timeout=timedelta(minutes=15),
            )
            if replacement.metadata.name is None:
                pytest.fail(f"Replacement Pod for leader {leader} has no name.")
            kubernetes_client.wait_for_pod_status(
                pod_name=replacement.metadata.name,
                namespace=target_model_ref.model,
                target_status=PodStatus.RUNNING,
                timeout=timedelta(minutes=15),
            )
        juju_client.multi_model_idle_for_period(models, timeout=timedelta(minutes=15))

    try:
        restart_leader()
        juju_client.validate_model(model=target_model_ref, level="deep", applications=[target_application])
        if neighbor_model_ref is not None and neighbor_application is not None:
            juju_client.validate_model(model=neighbor_model_ref, level="deep", applications=[neighbor_application])
    except Exception as operation_error:
        try:
            wait_for_recovery()
        except Exception as recovery_error:
            raise operation_error from recovery_error
        raise
    else:
        wait_for_recovery()
