import logging
from datetime import datetime, timedelta
from time import sleep

import pytest
from juju import JujuClient
from kubernetes import client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

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

    k8s_config.load_kube_config()

    v1 = client.CoreV1Api()

    target_pod_name = f"{target_application}-0"  # built off of the assumption that the charm is named target_application and the pod is named target_application-0
    all_pods = v1.list_pod_for_all_namespaces(watch=False)
    target_pod = next((pod for pod in all_pods.items if pod.metadata.name == target_pod_name), None)
    if not target_pod:
        pytest.fail(f"Could not find pod for application {target_application}")

    namespace = target_pod.metadata.namespace

    logger.info(f"Found target pod {target_pod_name} in namespace {namespace}. Deleting pod...")

    # Delete the target pod
    try:
        v1.delete_namespaced_pod(name=target_pod_name, namespace=namespace)
    except ApiException as e:
        pytest.fail(f"Exception when trying to delete pod: {e}")

    # Wait for the pod to be recreated
    sleep(5)  # give some time for the pod to be deleted before checking for its recreation
    start_time = datetime.now()
    while start_time + timedelta(minutes=5) > datetime.now():
        # get juju status first to make sure there's no race condition between calling the action and the pod being recreated
        application_status = juju_client.backend.get_application_status(model=model, application=target_application)
        logger.info(f"Current application status: {application_status}")

        try:
            target_pod = v1.read_namespaced_pod(name=target_pod_name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                # Pod has not been recreated yet
                sleep(10)
                continue
            else:
                pytest.fail(f"Exception when trying to read pod: {e}")

        if target_pod.status.phase == "Running":
            logger.info(f"Pod {target_pod_name} is running.")
            break
        else:
            if application_status == "active":
                logger.warning(f"Pod {target_pod_name} is not running, but application status is active.")
                pytest.fail(f"Pod {target_pod_name} is not running, but application status is active.")

        sleep(10)
    # kubernetes charms are defined as stateful sets, so the pod will be recreated. The check is if the charm is running correctly.

    else:
        pytest.fail(f"Pod {target_pod_name} was not recreated and running within the expected time.")

    # Wait for return to idle
    juju_client.idle_for_period(model=model, timeout=timedelta(minutes=15))

    # Validate all applications and relations
    juju_client.validate_model(model=model, level="simple")
