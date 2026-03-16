# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from datetime import datetime, timedelta
from enum import Enum
from time import sleep
from typing import Callable, List

from kubernetes import client as k8s_client  # type: ignore[import-untyped]
from kubernetes.client import ApiException  # type: ignore[import-untyped]

from .client import KubernetesClient


class PodStatus(Enum):
    PENDING = "Pending"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    UNKNOWN = "Unknown"


class KubernetesBackend:
    client: KubernetesClient

    def __init__(
        self,
        client: KubernetesClient | None = None,
        logger: logging.Logger | None = None,
        default_timeout: timedelta = timedelta(minutes=5),
        default_delay: timedelta = timedelta(seconds=1),
    ):
        self.client = client or KubernetesClient()
        self.logger = logger or logging.getLogger(__name__)

        self.default_timeout = default_timeout
        self.default_delay = default_delay

    def get_charm_pods(
        self, charm: str, model: str
    ) -> List[k8s_client.V1Pod]:  # multiple pods per a charm, depends on charm name and unit number
        """
        Gets all pods in the specified namespace that match the given charm name.
        Args:
            charm: Name of the charm to filter pods by
            model: Model charm is deployed in
        Raises:
            ApiException: If there is an error communicating with the Kubernetes API
        Returns:
            List of pods that match the given charm name in the specified namespace
        """
        pods = self.client.list_namespaced_pods(model)  # juju creates a namespace for each model
        matching_pods = [pod for pod in pods if pod.metadata.name.startswith(charm)]
        return matching_pods

    def wait(
        self,
        namespace: str,
        pod_name: str,
        ready: Callable[[PodStatus], bool],
        timeout: timedelta | None = None,
        delay: timedelta | None = None,
    ) -> None:
        """
        Wait for a pod to reach a desired status.

        Args:
            namespace: Namespace where the pod is located
            pod_name: Name of the pod to wait for
            ready: Callable that takes a PodStatus and returns True if the pod is ready
            timeout: Maximum time to wait
            delay: Delay between checks

        Raises:
            TimeoutError: If pod does not reach the desired status within timeout
        """
        timeout = timeout or self.default_timeout
        delay = delay or self.default_delay
        start_time = datetime.now()

        while True:
            if datetime.now() > start_time + timeout:
                self.logger.error(f"Timeout waiting for pod {pod_name} in namespace {namespace} to be ready.")
                raise TimeoutError(f"Timeout waiting for pod {pod_name} in namespace {namespace} to be ready.")

            try:
                pod = self.client.get_namespaced_pod(pod_name, namespace)

                if pod and ready(PodStatus(pod.status.phase)):
                    self.logger.info(f"Pod {pod.metadata.name} in namespace {namespace} is ready.")
                    return
            except ApiException:
                raise

            sleep(delay.total_seconds())

    def wait_for_pod_recreation(
        self,
        pod_name: str,
        namespace: str,
        old_uid: str,
        target_status: PodStatus = PodStatus.RUNNING,
        timeout: timedelta | None = None,
        delay: timedelta | None = None,
    ) -> k8s_client.V1Pod:
        """
        Wait for a pod to be recreated with a new UID and reach the target status.

        Args:
            pod_name: Name of the pod to wait for
            namespace: Namespace where the pod is located
            old_uid: UID of the old pod (to detect recreation)
            target_status: Desired pod status (default: RUNNING)
            timeout: Maximum time to wait
            delay: Delay between checks

        Returns:
            The recreated pod object

        Raises:
            TimeoutError: If pod is not recreated within timeout
        """
        timeout = timeout or self.default_timeout
        delay = delay or self.default_delay
        start_time = datetime.now()

        self.logger.info(f"Waiting for pod {pod_name} to be recreated (old UID: {old_uid})")

        while datetime.now() < start_time + timeout:
            try:
                new_pod = self.client.get_namespaced_pod(pod_name, namespace)
                if new_pod.metadata.uid == old_uid:
                    # Same pod, not recreated yet
                    sleep(delay.total_seconds())
                    continue

                # Pod has been recreated with new UID
                elif PodStatus(new_pod.status.phase) == target_status:
                    self.logger.info(
                        f"Pod {pod_name} in namespace {namespace} recreated successfully with UID {new_pod.metadata.uid} "
                        f"and status {target_status.value}"
                    )
                    return new_pod
                else:
                    self.logger.debug(
                        f"Pod {pod_name} in namespace {namespace} recreated with new UID but status is {new_pod.status.phase}, "
                        f"waiting for {target_status.value}"
                    )

            except ApiException as e:
                if e.status == 404:
                    sleep(delay.total_seconds())
                    continue
                else:
                    raise e

            sleep(delay.total_seconds())

        raise TimeoutError(
            f"Pod {pod_name} in namespace {namespace} was not recreated and reached {target_status.value} status within timeout"
        )
