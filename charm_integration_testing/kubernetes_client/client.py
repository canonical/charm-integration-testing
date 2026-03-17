# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import re
from datetime import datetime, timedelta
from enum import Enum
from time import sleep
from typing import Callable, TypeVar

from kubernetes import client as K8sClient  # type: ignore[import-untyped]
from kubernetes.client import ApiException  # type: ignore[import-untyped]

T = TypeVar("T")


class PodStatus(Enum):
    PENDING = "Pending"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    UNKNOWN = "Unknown"


class KubernetesClient:
    backend: K8sClient.CoreV1Api

    def __init__(
        self,
        backend: K8sClient.CoreV1Api,
        logger: logging.Logger | None = None,
        default_timeout: timedelta = timedelta(minutes=5),
        default_delay: timedelta = timedelta(seconds=1),
    ):
        self.backend = backend
        self.logger = logger or logging.getLogger(__name__)

        self.default_timeout = default_timeout
        self.default_delay = default_delay

    def get_charm_pods(
        self, application_name: str, model: str
    ) -> list[K8sClient.V1Pod]:  # multiple pods per a charm, depends on charm name and unit number
        """
        Gets all pods in the specified namespace that match the given application name.
        Args:
            application_name: Name of the application to filter pods by
            model: Model the application is deployed in
        Raises:
            ApiException: If there is an error communicating with the Kubernetes API
        Returns:
            List of pods that match the given application name in the specified namespace
        """
        pattern = re.compile(rf"^{re.escape(application_name)}(-\d+)$")
        pods = self.backend.list_namespaced_pod(model)  # juju creates a namespace for each model
        matching_pods = [pod for pod in pods.items if pattern.match(pod.metadata.name)]
        return matching_pods

    def wait(
        self,
        check: Callable[[], T | None],
        timeout_message: str,
        timeout: timedelta | None = None,
        delay: timedelta | None = None,
    ) -> T:
        """
        Generic polling loop that repeatedly calls a check function until it returns a non-None result.

        Args:
            check: A callable that returns a non-None value on success or None to keep polling
            timeout_message: Message for the TimeoutError if polling exceeds the timeout
            timeout: Maximum time to wait
            delay: Delay between checks

        Returns:
            The non-None result from the check function

        Raises:
            TimeoutError: If check does not succeed within timeout
        """
        timeout = timeout or self.default_timeout
        delay = delay or self.default_delay
        start_time = datetime.now()

        while True:
            if datetime.now() > start_time + timeout:
                self.logger.error(timeout_message)
                raise TimeoutError(timeout_message)

            result = check()
            if result is not None:
                return result

            sleep(delay.total_seconds())

    def wait_for_pod_recreation(
        self,
        pod_name: str,
        namespace: str,
        old_uid: str,
        target_status: PodStatus = PodStatus.RUNNING,
        timeout: timedelta | None = None,
        delay: timedelta | None = None,
    ) -> K8sClient.V1Pod:
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
        self.logger.info(f"Waiting for pod {pod_name} to be recreated (old UID: {old_uid})")

        def check() -> K8sClient.V1Pod | None:
            try:
                new_pod = self.backend.read_namespaced_pod(pod_name, namespace)
                if new_pod.metadata.uid == old_uid:
                    return None

                if PodStatus(new_pod.status.phase) == target_status:
                    self.logger.info(
                        f"Pod {pod_name} in namespace {namespace} recreated successfully with UID {new_pod.metadata.uid} "
                        f"and status {target_status.value}"
                    )
                    return new_pod

                self.logger.debug(
                    f"Pod {pod_name} in namespace {namespace} recreated with new UID but status is {new_pod.status.phase}, "
                    f"waiting for {target_status.value}"
                )
                return None
            except ApiException as e:
                if e.status == 404:
                    return None
                raise

        return self.wait(
            check=check,
            timeout_message=f"Pod {pod_name} in namespace {namespace} was not recreated and reached {target_status.value} status within timeout",
            timeout=timeout,
            delay=delay,
        )

    def delete_pod(self, namespace: str, pod_name: str) -> None:
        """
        Deletes the specified pod.

        Args:
            namespace: Namespace where the pod is located
            pod_name: Name of the pod to delete

        Raises:
            ApiException: If there is an error communicating with the Kubernetes API
        """
        try:
            self.backend.delete_namespaced_pod(name=pod_name, namespace=namespace)
            self.logger.info(f"Pod {pod_name} in namespace {namespace} has been deleted.")
        except ApiException as e:
            self.logger.error(f"Failed to delete pod {pod_name} in namespace {namespace}: {e}")
            raise
