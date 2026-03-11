# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import datetime, timedelta
from time import sleep

import pytest
from juju import JujuClient
from kubernetes import client  # type: ignore[import-untyped]
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException  # type: ignore[import-untyped]

from .scheduler.states import State

logger = logging.getLogger(__name__)


@pytest.mark.state(requires=State.DEPLOYED, provides=State.DEPLOYED)
def test_pod_deletion(juju_client: JujuClient, model: str, target_application: str) -> None:
    """
    This test validates that if a pod is deleted outside of Juju, Juju will recreate the pod and the application will return to an idle state.

    steps:
    1. find the pod associated with the target application
    2. delete the pod
    3. wait for the pod to be recreated and return to a running state
    4. validate that the model is idle and healthy
    """

    if not juju_client.backend.is_k8s_model(model):
        pytest.skip("This test is only applicable for Kubernetes models.")

    try:
        k8s_config.load_kube_config()
    except k8s_config.ConfigException:
        pytest.skip("Could not load kubeconfig. Ensure that kubeconfig is present and valid to run this test.")

    v1 = client.CoreV1Api()

    target_pod_name = f"{target_application}-0"  # built off of the assumption that the charm is named target_application and the pod is named target_application-0
    namespace = model
    old_pod = None
    try:
        old_pod = v1.read_namespaced_pod(name=target_pod_name, namespace=namespace)
    except ApiException as e:
        pytest.fail(f"Exception when trying to read pod: {e}")

    logger.info(
        f"Found target pod {target_pod_name} in namespace {namespace}, UID {old_pod.metadata.uid}. Deleting pod..."
    )

    # Delete the target pod
    try:
        v1.delete_namespaced_pod(name=target_pod_name, namespace=namespace)
    except ApiException as e:
        pytest.fail(f"Exception when trying to delete pod: {e}")

    start_time = datetime.now()
    while start_time + timedelta(minutes=5) > datetime.now():
        try:
            new_pod = v1.read_namespaced_pod(name=target_pod_name, namespace=namespace)
            if new_pod.metadata.uid == old_pod.metadata.uid:
                sleep(1)
                continue  # The pod has not been recreated yet, it's the same pod with the same UID
        except ApiException as e:
            if e.status == 404:
                # Pod has not been recreated yet
                sleep(0.25)
                continue
            else:
                pytest.fail(f"Exception when trying to read pod: {e}")

        if new_pod.status.phase == "Running":
            logger.info(f"Pod {target_pod_name} is running. UID {new_pod.metadata.uid}.")
            break

        sleep(0.1)
    else:
        pytest.fail(f"Pod {target_pod_name} was not recreated and running within the expected time.")

    logger.info(juju_client.backend.juju_status_text(model=model))
    # Wait for return to idle
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=5))

    # Validate all applications and relations
    juju_client.validate_model(model=model, level="simple")
