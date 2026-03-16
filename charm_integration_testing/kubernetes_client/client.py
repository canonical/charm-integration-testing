# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from logging import Logger

from kubernetes import client, config  # type: ignore[import-untyped]


class KubernetesClient:
    def __init__(self, kubeconfig_path: str | None = None, logger: Logger | None = None):
        if kubeconfig_path:
            config.load_kube_config(config_file=kubeconfig_path)
        else:
            config.load_kube_config()

        self.logger = logger or Logger(__name__)
        self.client = client.CoreV1Api()

    def list_namespaced_pods(self, namespace: str) -> list[client.V1Pod]:
        """
        List all pods in the specified namespace.
        Args:
            namespace: Namespace to list pods from
        Returns:
            List of pods in the specified namespace
        """

        return self.client.list_namespaced_pod(namespace)

    def list_namespaced_pod(self, namespace: str) -> client.V1PodList:
        """      
        Get a pod by name in the specified namespace.
        Args:
            namespace: Namespace to list pods from
        Returns:
            specified pod in  the specified namespace
        """
        return self.client.list_namespaced_pod(namespace)

    def delete_pod(self, namespace: str, pod_name: str) -> None:
        """
        Delete a pod by name in the specified namespace.
        Returns immediately after sending the delete request, does not wait for pod to be fully deleted.
        Args:
            namespace: Namespace where the pod is located
            pod_name: Name of the pod to delete
        Raises:
            ApiException: If there is an error communicating with the Kubernetes API
        Returns:
            None
        """
        self.client.delete_namespaced_pod(pod_name, namespace)

    def get_namespaced_pod(self, pod_name: str, namespace: str) -> client.V1Pod:
        """
        Get's the desired pod from the specified namespace.

        Args:
            pod_name: Name of the pod to retrieve
            namespace: Namespace where the pod is located

        Raises:
            ApiException: If there is an error communicating with the Kubernetes API
        Returns:
            The pod object if found
        """

        return self.client.read_namespaced_pod(name=pod_name, namespace=namespace)
